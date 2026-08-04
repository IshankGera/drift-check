from drift_check.models import CallSite, SignatureDefinition, Finding

class RuleEngine:
    def evaluate(self, call: CallSite, sig: SignatureDefinition) -> list[Finding]:
        findings = []

        # Rule 1: Unknown Signatures (Diagnostic Messages)
        if sig.is_unknown:
            if sig.error_code == "PACKAGE_NOT_INSTALLED":
                msg = f"Package '{sig.package_name}' not installed in target environment."
            elif sig.error_code == "METHOD_NOT_FOUND":
                msg = f"Method '{sig.method_name}' not found on '{sig.package_name}'."
            elif sig.error_code == "SIGNATURE_UNAVAILABLE":
                msg = f"Signature unavailable (likely C-extension). Cannot verify."
            else:
                msg = "Unsupported callable or unknown resolution error."
                
            findings.append(Finding(
                severity="WARNING",
                file_path=call.file_path,
                line_number=call.line_number,
                package_name=call.package_name,
                method_name=call.method_name,
                message=msg
            ))
            return findings

        # Rule 2: Dynamic kwargs at the call site (e.g., func(**my_dict))
        if call.has_dynamic_kwargs:
            findings.append(Finding(
                severity="WARNING",
                file_path=call.file_path,
                line_number=call.line_number,
                package_name=call.package_name,
                method_name=call.method_name,
                message="Dynamic **kwargs detected. Cannot statically verify all parameters."
            ))

        # Rule 3: Parameter Matching & Forwarding
        passed_ok = True
        for kwarg in call.kwargs_passed:
            if kwarg not in sig.valid_parameters:
                passed_ok = False
                
                # Apply strict forwarding constraint
                if sig.accepts_kwargs:
                    findings.append(Finding(
                        severity="WARNING",
                        file_path=call.file_path,
                        line_number=call.line_number,
                        package_name=call.package_name,
                        method_name=call.method_name,
                        message=f"Unable to verify forwarded keyword argument: '{kwarg}'"
                    ))
                else:
                    findings.append(Finding(
                        severity="ERROR",
                        file_path=call.file_path,
                        line_number=call.line_number,
                        package_name=call.package_name,
                        method_name=call.method_name,
                        message=f"Unknown parameter passed: '{kwarg}'"
                    ))
        
        if passed_ok and not call.has_dynamic_kwargs:
             findings.append(Finding(
                severity="OK",
                file_path=call.file_path,
                line_number=call.line_number,
                package_name=call.package_name,
                method_name=call.method_name,
                message="All parameters valid."
            ))

        return findings