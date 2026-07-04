import sys
import json

def main():
    try:
        # Read the event context passed via stdin
        input_data = json.load(sys.stdin)
    except Exception as e:
        # Safe default: allow but log error internally
        print(json.dumps({
            "allow_tool": True,
            "decision": "ALLOW",
            "reason": f"Failed to parse stdin as JSON: {e}"
        }))
        sys.exit(0)

    command_line = ""

    # Locate the CommandLine argument from the toolCall structure
    tool_call = input_data.get("toolCall", {})
    args = tool_call.get("args") or tool_call.get("arguments") or {}

    if isinstance(args, dict):
        command_line = args.get("CommandLine", "")

    # Fallback recursive search for CommandLine
    if not command_line:
        def find_command_line(obj):
            if isinstance(obj, dict):
                if "CommandLine" in obj:
                    return obj["CommandLine"]
                for val in obj.values():
                    res = find_command_line(val)
                    if res:
                        return res
            elif isinstance(obj, list):
                for item in obj:
                    res = find_command_line(item)
                    if res:
                        return res
            return ""
        command_line = find_command_line(input_data)

    command_line_str = str(command_line).strip().lower()

    # Define patterns for destructive commands
    destructive_patterns = [
        "rm -rf /",
        "rm -rf *",
        "rm -fr /",
        "rm -fr *",
        "rm -r /",
        "rm -r *",
        "format c:",
        "del /f /s /q c:\\",
    ]

    is_destructive = False
    triggered_pattern = ""
    for pattern in destructive_patterns:
        # Normalize double spaces and check
        if pattern in command_line_str.replace("  ", " "):
            is_destructive = True
            triggered_pattern = pattern
            break

    if is_destructive:
        response = {
            "allow_tool": False,
            "decision": "DENY",
            "deny_reason": f"Execution of destructive command containing '{triggered_pattern}' is prohibited."
        }
    else:
        response = {
            "allow_tool": True,
            "decision": "ALLOW"
        }

    print(json.dumps(response))
    sys.exit(0)

if __name__ == "__main__":
    main()
