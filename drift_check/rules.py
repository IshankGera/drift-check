from drift_check.models import CallSite, SignatureDefinition, Finding

class RuleEngine:
    def evaluate(self, call: CallSite, sig: SignatureDefinition) -> list[Finding]:
        findings = []

        # 1. Method-Level Impact Analysis
        if sig.is_unknown:
            if sig.error_code == "METHOD_NOT_FOUND":
                msg = f"Method '{sig.method_name}' no longer exists in the target version."
                findings.append(Finding(
                    severity="ERROR",
                    file_path=call.file_path,
                    line_number=call.line_number,
                    package_name=call.package_name,
                    method_name=call.method_name,
                    message=msg
                ))
            elif sig.error_code == "PACKAGE_NOT_INSTALLED":
                msg = f"Failed to inspect target sandbox for '{sig.package_name}'."
                findings.append(Finding("WARNING", call.file_path, call.line_number, call.package_name, call.method_name, msg))
            elif sig.error_code == "SIGNATURE_UNAVAILABLE":
                msg = "Signature unavailable in target version (likely C-extension). Cannot verify compatibility."
                findings.append(Finding("WARNING", call.file_path, call.line_number, call.package_name, call.method_name, msg))
            return findings

        # 2. Dynamic Kwargs at Call Site (User Code Limitations)
        if call.has_dynamic_kwargs:
            findings.append(Finding(
                severity="WARNING",
                file_path=call.file_path,
                line_number=call.line_number,
                package_name=call.package_name,
                method_name=call.method_name,
                message="Dynamic **kwargs detected in your code. Cannot statically verify upgrade compatibility."
            ))

        # 3. Parameter-Level Intersection
        for kwarg in call.kwargs_passed:
            if kwarg not in sig.valid_parameters:
                
                # Apply the strict kwargs honesty policy
                if sig.accepts_kwargs:
                    findings.append(Finding(
                        severity="WARNING",
                        file_path=call.file_path,
                        line_number=call.line_number,
                        package_name=call.package_name,
                        method_name=call.method_name,
                        message=f"Parameter '{kwarg}' could not be verified. Target API uses dynamic **kwargs. (Confidence: LOW)"
                    ))
                else:
                    # Parameter actively removed in target version
                    findings.append(Finding(
                        severity="ERROR",
                        file_path=call.file_path,
                        line_number=call.line_number,
                        package_name=call.package_name,
                        method_name=call.method_name,
                        message=f"Parameter '{kwarg}' is no longer supported."
                    ))

        return findings