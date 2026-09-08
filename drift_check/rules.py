from drift_check.models import CallSite, SignatureDefinition, Finding

class RuleEngine:
    def evaluate(self, call: CallSite, sig: SignatureDefinition) -> list[Finding]:
        findings = []

        # 1. Method-level compatibility
        if sig.is_unknown:
            if sig.error_code == "METHOD_NOT_FOUND":
                findings.append(Finding(
                    severity="ERROR",
                    file_path=call.file_path,
                    line_number=call.line_number,
                    package_name=call.package_name,
                    method_name=call.method_name,
                    message=f"Method '{sig.method_name}' no longer exists in the target version."
                ))

            elif sig.error_code == "PACKAGE_NOT_INSTALLED":
                findings.append(Finding(
                    severity="WARNING",
                    file_path=call.file_path,
                    line_number=call.line_number,
                    package_name=call.package_name,
                    method_name=call.method_name,
                    message=f"Failed to inspect target sandbox for '{sig.package_name}'."
                ))

            elif sig.error_code == "SIGNATURE_UNAVAILABLE":
                findings.append(Finding(
                    severity="WARNING",
                    file_path=call.file_path,
                    line_number=call.line_number,
                    package_name=call.package_name,
                    method_name=call.method_name,
                    message="Signature unavailable in target version. Cannot verify compatibility."
                ))

            else:
                findings.append(Finding(
                    severity="WARNING",
                    file_path=call.file_path,
                    line_number=call.line_number,
                    package_name=call.package_name,
                    method_name=call.method_name,
                    message="Unable to determine target API compatibility."
                ))

            return findings

        # 2. Dynamic **kwargs
        #
        # We cannot know which parameters are actually passed,
        # so don't perform parameter-level validation.
        if call.has_dynamic_kwargs:
            findings.append(Finding(
                severity="WARNING",
                file_path=call.file_path,
                line_number=call.line_number,
                package_name=call.package_name,
                method_name=call.method_name,
                message=(
                    "Dynamic **kwargs detected in your code. "
                    "Cannot statically verify parameter compatibility."
                )
            ))

            return findings

        # 3. Known keyword arguments
        for kwarg in call.kwargs_passed:

            if kwarg in sig.valid_parameters:
                continue

            if sig.accepts_kwargs:
                findings.append(Finding(
                    severity="WARNING",
                    file_path=call.file_path,
                    line_number=call.line_number,
                    package_name=call.package_name,
                    method_name=call.method_name,
                    message=(
                        f"Parameter '{kwarg}' could not be verified. "
                        "Target API accepts dynamic **kwargs. "
                        "(Confidence: LOW)"
                    )
                ))

            else:
                findings.append(Finding(
                    severity="ERROR",
                    file_path=call.file_path,
                    line_number=call.line_number,
                    package_name=call.package_name,
                    method_name=call.method_name,
                    message=f"Parameter '{kwarg}' is no longer supported."
                ))

        return findings