import traceback
import sys
import logging

logger = logging.getLogger("bashmanager.errors")

class BashManagerError(Exception):
    """Base class for specific categorized errors."""
    code = "UNKNOWN_ERROR"
    status_code = 500

    def __init__(self, message, context=None, suggestion=None):
        super().__init__(message)
        self.message = message
        self.context = context or {}
        self.suggestion = suggestion
        self.stack_trace = traceback.format_exc()

    def to_dict(self):
        return {
            "error_type": self.__class__.__name__,
            "error_code": self.code,
            "message": self.message,
            "context": self.context,
            "suggestion": self.suggestion,
            "stack_trace": self.stack_trace,
        }

class ScriptSyntaxError(BashManagerError):
    code = "ERR_SYNTAX"
    status_code = 400

class ScriptRuntimeError(BashManagerError):
    code = "ERR_RUNTIME"
    status_code = 500

class ScriptTimeoutError(BashManagerError):
    code = "ERR_TIMEOUT"
    status_code = 408

class NotFoundError(BashManagerError):
    code = "ERR_NOT_FOUND"
    status_code = 404

class PermissionDeniedError(BashManagerError):
    code = "ERR_PERMISSION_DENIED"
    status_code = 403

def format_exception_details(e: Exception):
    """Format any Python exception into a structured, categorized error response."""
    
    # If it's our custom error, we already have details
    if isinstance(e, BashManagerError):
        details = e.to_dict()
    else:
        # Categorize built-in exceptions
        error_type = type(e).__name__
        suggestion = "Check the server logs and stack trace for more details."
        code = "ERR_INTERNAL"
        
        if isinstance(e, SyntaxError):
            code = "ERR_SYNTAX"
            suggestion = "Check the code for missing brackets, quotes, or invalid Python syntax."
        elif isinstance(e, TimeoutError):
            code = "ERR_TIMEOUT"
            suggestion = "The operation took too long. Try increasing the timeout or optimizing the process."
        elif isinstance(e, FileNotFoundError):
            code = "ERR_NOT_FOUND"
            suggestion = "Verify that the specified file or directory path is correct and exists."
        elif isinstance(e, PermissionError):
            code = "ERR_PERMISSION_DENIED"
            suggestion = "Ensure the application has read/write permissions for the requested resource."
        elif isinstance(e, KeyError):
            code = "ERR_MISSING_KEY"
            suggestion = f"A required key or parameter was missing: {str(e)}"
        elif isinstance(e, ValueError):
            code = "ERR_INVALID_VALUE"
            suggestion = "An invalid value was provided. Please check the input arguments."

        # Include local variables from the traceback for context if possible
        context = {}
        tb = e.__traceback__
        if tb:
            while tb.tb_next:
                tb = tb.tb_next
            # Be careful not to expose sensitive information (passwords, etc)
            frame_locals = tb.tb_frame.f_locals
            for k, v in frame_locals.items():
                if "pass" not in k.lower() and "secret" not in k.lower() and "key" not in k.lower():
                    try:
                        context[k] = repr(v)[:100]  # Truncate large values
                    except Exception:
                        pass
                        
        details = {
            "error_type": error_type,
            "error_code": code,
            "message": str(e) or "An unexpected error occurred",
            "context": context,
            "suggestion": suggestion,
            "stack_trace": traceback.format_exc(),
        }

    logger.error(f"Structured Error [{details['error_code']}]: {details['message']}\nTraceback:\n{details['stack_trace']}")
    return details

def analyze_script_output_error(line):
    """Analyze script stdout/stderr lines to categorize common script failures."""
    line_lower = line.lower()
    
    # Syntax Errors
    if any(err in line_lower for err in ["syntax error", "unexpected token", "missing", "expected"]):
        return {
            "type": "SyntaxError",
            "code": "ERR_SCRIPT_SYNTAX",
            "message": line.strip(),
            "suggestion": "Check the script for missing quotes, parentheses, or incorrect syntax."
        }
        
    # Permission Errors
    if any(err in line_lower for err in ["permission denied", "access denied", "eacces"]):
        return {
            "type": "PermissionError",
            "code": "ERR_SCRIPT_PERMISSION",
            "message": line.strip(),
            "suggestion": "Ensure the script is executable (chmod +x) or run with elevated privileges."
        }
        
    # File Not Found Errors
    if any(err in line_lower for err in ["not found", "no such file or directory", "enoent", "command not found"]):
        return {
            "type": "NotFoundError",
            "code": "ERR_SCRIPT_NOT_FOUND",
            "message": line.strip(),
            "suggestion": "Verify the file path exists and that required commands are installed."
        }
        
    # Timeout Errors
    if any(err in line_lower for err in ["timeout", "timed out"]):
        return {
            "type": "TimeoutError",
            "code": "ERR_SCRIPT_TIMEOUT",
            "message": line.strip(),
            "suggestion": "The script operation took too long. Try optimizing the script."
        }
        
    # Default Runtime Error
    if any(err in line_lower for err in ["error:", "failed:", "exception", "traceback"]):
        return {
            "type": "RuntimeError",
            "code": "ERR_SCRIPT_RUNTIME",
            "message": line.strip(),
            "suggestion": "Review the script error message and check variable states."
        }
        
    return None
