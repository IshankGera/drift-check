from typing import List, Optional, Set, Tuple

from drift_check.models import CallSite, Finding, SignatureDefinition

# (kind, detail): identifies an argument problem independently of the message wording,
# so the same problem can be looked up against the baseline version.
Issue = Tuple[str, str]



class RuleEngine:
    def evaluate(
        self,
        call: CallSite,
        sig: SignatureDefinition,
        baseline: Optional[SignatureDefinition] = None,
    ) -> List[Finding]:
        """
        Compares one call site with the target version's signature.

        `baseline` is the signature in the currently installed version. When given, every
        problem is only reported if today's version does not have it too. That keeps the
        report to what the upgrade itself breaks, and filters out analysis mistakes.
        """
        def finding(severity: str, message: str) -> Finding:
            return Finding(severity, call.file_path, call.line_number,
                           call.package_name, call.method_name, message, kind=call.kind)

        if call.kind == "import":
            return self._evaluate_import(call, sig, baseline, finding)

        findings: List[Finding] = []

        # 1. Does the method still exist?
        if sig.is_unknown:
            if sig.error_code == "METHOD_NOT_FOUND":
                if baseline is not None and baseline.error_code == "METHOD_NOT_FOUND":
                    return []  # does not resolve today either: already broken, or not really a package call
                return [finding("ERROR", f"Method '{sig.method_name}' no longer exists in the target version.")]
            if sig.error_code == "PACKAGE_NOT_INSTALLED":
                findings.append(finding("WARNING", f"Failed to inspect target sandbox for '{sig.package_name}'."))
            elif sig.error_code == "IMPORT_FAILED":
                findings.append(finding("WARNING", f"'{sig.package_name}' could not be imported in the target sandbox. "
                                                   f"Cannot verify '{sig.method_name}'."))
            elif sig.error_code == "UNRESOLVED":
                findings.append(finding("WARNING", f"Could not resolve '{sig.method_name}' through an instance attribute "
                                                   f"without type hints. (Confidence: LOW)"))
            elif sig.error_code == "SIGNATURE_UNAVAILABLE":
                findings.append(finding("WARNING", "Signature unavailable in target version. Cannot verify compatibility."))
            else:
                findings.append(finding("WARNING", "Unable to determine target API compatibility."))
            findings.extend(self._deprecation(sig, baseline, finding))
            return findings

        # 2. Deprecation (still works, but will be removed later)
        findings.extend(self._deprecation(sig, baseline, finding))

        # 3. Dynamic **kwargs at the call site: parameter checks are not possible
        if call.has_dynamic_kwargs:
            if call.kind != "constructor":
                findings.append(finding("WARNING", "Dynamic **kwargs detected in your code. "
                                                   "Cannot statically verify parameter compatibility."))
            return findings

        # 4. Keyword arguments
        for kwarg in call.kwargs_passed:
            if kwarg in sig.valid_parameters:
                continue
            if sig.accepts_kwargs:
                # Constructors with **kwargs (e.g. data models) would be all noise
                if call.kind != "constructor":
                    findings.append(finding("WARNING", f"Parameter '{kwarg}' could not be verified. "
                                                       "Target API accepts dynamic **kwargs. (Confidence: LOW)"))
            elif baseline is not None and not baseline.is_unknown \
                    and kwarg not in baseline.valid_parameters and not baseline.accepts_kwargs:
                continue  # not accepted today either: not caused by the upgrade
            else:
                findings.append(finding("ERROR", f"Parameter '{kwarg}' is no longer supported."))

        # 5. Positional count and required parameters
        new_issues = self.argument_issues(call, sig)
        if baseline is not None and not baseline.is_unknown:
            new_issues -= self.argument_issues(call, baseline)
        for kind, detail in sorted(new_issues):
            if kind == "too_many_positional":
                findings.append(finding("ERROR", f"Takes at most {sig.max_positional} positional argument(s) in the "
                                                 f"target version, but {call.positional_count} are passed."))
            elif kind == "missing_required":
                findings.append(finding("ERROR", f"Required parameter '{detail}' is not passed "
                                                 f"(it is required in the target version)."))
        return findings

    @staticmethod
    def argument_issues(call: CallSite, sig: SignatureDefinition) -> Set[Issue]:
        """Problems with how many/which arguments are passed. Empty when the shape is unknown."""
        issues: Set[Issue] = set()
        if sig.is_unknown or call.has_star_args or call.has_dynamic_kwargs:
            return issues
        if sig.max_positional is not None and call.positional_count > sig.max_positional:
            issues.add(("too_many_positional", ""))
        filled = set(sig.positional_names[: call.positional_count]) | set(call.kwargs_passed)
        for name in sig.required_params:
            if name not in filled:
                issues.add(("missing_required", name))
        return issues

    @staticmethod
    def _deprecation(sig, baseline, finding) -> List[Finding]:
        if not sig.deprecated:
            return []
        note = " (already deprecated in your current version)" if baseline and baseline.deprecated else ""
        message = " ".join(sig.deprecated.split())
        if len(message) > 200:
            message = message[:197] + "..."
        return [finding("WARNING", f"Deprecated in the target version{note}: {message}")]

    def _evaluate_import(self, call, sig, baseline, finding) -> List[Finding]:
        """Imports only need to exist in the target version."""
        if sig.error_code == "METHOD_NOT_FOUND":
            if baseline is not None and baseline.error_code == "METHOD_NOT_FOUND":
                return []  # does not exist today either: not caused by the upgrade
            if call.guarded:
                return [finding("WARNING", f"Import '{call.method_name}' from '{call.package_name}' no longer exists in "
                                           f"the target version. It is inside try/except ImportError, so your "
                                           f"fallback will be used: check that the fallback still works.")]
            return [finding("ERROR", f"Import '{call.method_name}' from '{call.package_name}' "
                                     f"no longer exists in the target version.")]
        if sig.error_code == "IMPORT_FAILED":
            return [finding("WARNING", f"'{call.package_name}' could not be imported in the target sandbox. "
                                       f"Cannot verify this import.")]
        return self._deprecation(sig, baseline, finding)

    @staticmethod
    def needs_baseline(call: CallSite, sig: SignatureDefinition) -> bool:
        """True when the target version shows a problem that must be confirmed against today's version."""
        if sig.error_code == "METHOD_NOT_FOUND" or sig.deprecated:
            return True
        if call.kind == "import" or sig.is_unknown:
            return False
        if not sig.accepts_kwargs and any(k not in sig.valid_parameters for k in call.kwargs_passed):
            return True
        return bool(RuleEngine.argument_issues(call, sig))


def suppress_consequences(findings: List[Finding]) -> List[Finding]:
    """
    If `from pkg import old_fn` fails (or is deprecated), every `old_fn()` call in that file
    has the same problem. Report it once, at the import, instead of once per call.
    """
    def keys(kind_prefix: str):
        return {
            (f.file_path, f.package_name, f.method_name)
            for f in findings
            if f.kind == "import" and f.message.startswith(kind_prefix)
        }

    missing = keys("Import ")
    deprecated = keys("Deprecated")

    def covered_by(f: Finding, import_keys) -> bool:
        return any(
            (f.file_path, f.package_name) == (fp, pkg)
            and (f.method_name == path or f.method_name.startswith(path + "."))
            for fp, pkg, path in import_keys
        )

    kept = []
    for f in findings:
        if f.kind != "import":
            if "no longer exists" in f.message and covered_by(f, missing):
                continue
            if f.message.startswith("Deprecated") and covered_by(f, deprecated):
                continue
        kept.append(f)
    return kept
