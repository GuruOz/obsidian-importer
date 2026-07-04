#!/usr/bin/env python3
"""Custom tool-calling agent loop, powered by OpenAI-compatible APIs (OpenRouter, DeepSeek, etc)."""
import fnmatch
import json
import os
import re
import sys
import traceback
from datetime import datetime
from openai import OpenAI

# Environment setup
VAULT_DIR = os.path.normpath(os.environ.get("VAULT_DIR", "/vault"))
STAGING_DIR = os.path.normpath(os.environ.get("STAGING_DIR", "/work/staging"))
DRY_RUN = os.environ.get("DRY_RUN", "1") == "1"

def safe_path(p: str) -> str:
    """Ensure path is within the allowed directories (VAULT_DIR or STAGING_DIR)."""
    if not os.path.isabs(p):
        p = os.path.join(VAULT_DIR, p)
    p = os.path.normpath(p)
    # Bare startswith would also match sibling dirs (e.g. "/vault2" vs "/vault"),
    # so require an exact root match or a path-separator boundary.
    if not any(p == root or p.startswith(root + os.sep) for root in (VAULT_DIR, STAGING_DIR)):
        raise ValueError(f"Access denied: {p}")
    return p

# --- Tools Implementation ---

def read_file(path: str) -> str:
    """Read the contents of a file."""
    try:
        p = safe_path(path)
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Error reading file: {e}"

def glob_search(pattern: str) -> str:
    """Find files in the vault matching a glob pattern."""
    matches = []
    for root, _, files in os.walk(VAULT_DIR):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), VAULT_DIR)
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(f, pattern):
                matches.append(rel)
    return "\n".join(matches) if matches else "No files found."

def grep_search(query: str) -> str:
    """Search for a regex string inside all markdown files in the vault."""
    matches = []
    try:
        regex = re.compile(query, re.IGNORECASE)
        for root, _, files in os.walk(VAULT_DIR):
            for f in files:
                if f.endswith(".md"):
                    p = os.path.join(root, f)
                    try:
                        with open(p, "r", encoding="utf-8", errors="ignore") as fobj:
                            for i, line in enumerate(fobj):
                                if regex.search(line):
                                    rel = os.path.relpath(p, VAULT_DIR)
                                    matches.append(f"{rel}:{i+1}: {line.strip()}")
                                    if len(matches) > 100:
                                        matches.append("... [too many matches, truncated]")
                                        return "\n".join(matches)
                    except Exception:
                        pass
        return "\n".join(matches) if matches else "No matches found."
    except Exception as e:
        return f"Error in grep: {e}"

def write_file(path: str, content: str) -> str:
    """Write or overwrite a file."""
    # Filing_Rules.md is exempt so the run-once profiler works before go-live (M5).
    if DRY_RUN and not path.endswith(("proposed.md", "Filing_Rules.md")):
        return f"Error: DRY_RUN is enabled. You can only write to proposed.md in staging."
    try:
        p = safe_path(path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error writing file: {e}"

def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Replace exactly one occurrence of old_string with new_string in a file."""
    if DRY_RUN:
        return "Error: DRY_RUN is enabled. You cannot edit vault files."
    try:
        p = safe_path(path)
        with open(p, "r", encoding="utf-8") as f:
            content = f.read()
        
        if content.count(old_string) == 0:
            return "Error: old_string not found in file."
        elif content.count(old_string) > 1:
            return "Error: old_string occurs multiple times. Please provide a more specific old_string."
            
        new_content = content.replace(old_string, new_string)
        with open(p, "w", encoding="utf-8") as f:
            f.write(new_content)
        return f"Successfully edited {path}"
    except Exception as e:
        return f"Error editing file: {e}"


# --- Tool Schemas ---
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path to the file (relative to vault)"}},
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "glob_search",
            "description": "Find files in the vault matching a glob pattern (e.g. '*.md' or 'Work/*').",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "grep_search",
            "description": "Search for a regex string inside all markdown files in the vault.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write new content to a file (overwrites if it exists).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace exactly one occurrence of old_string with new_string in a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string", "description": "The exact string to replace. Must match exactly including whitespace."},
                    "new_string": {"type": "string", "description": "The string to replace it with."}
                },
                "required": ["path", "old_string", "new_string"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Call this when you have successfully completed the task to output the final status JSON.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "work_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "files_touched": {"type": "array", "items": {"type": "string"}},
                    "entries_filed": {"type": "integer"},
                    "sections": {"type": "integer"}
                },
                "required": ["status"]
            }
        }
    }
]

def main():
    api_key = os.environ.get("LLM_API_KEY")
    base_url = os.environ.get("LLM_BASE_URL")
    model = os.environ.get("LLM_MODEL")
    prompt_file = sys.argv[1]

    if not all([api_key, base_url, model]):
        print("ERROR: LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL must be set for custom agent.", file=sys.stderr)
        sys.exit(1)

    client = OpenAI(api_key=api_key, base_url=base_url)

    with open(prompt_file, "r", encoding="utf-8") as f:
        prompt = f.read()

    system_content = "You are an autonomous file-management agent for an Obsidian vault."
    # Inject the vault's conventions manifest (generated by profile_vault.py) so every
    # run is guaranteed to see it, rather than relying on the model choosing to read it.
    filing_rules_path = os.path.join(VAULT_DIR, "Filing_Rules.md")
    if os.path.exists(filing_rules_path):
        with open(filing_rules_path, "r", encoding="utf-8") as f:
            system_content += "\n\nVault conventions (Filing_Rules.md):\n\n" + f.read()

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": prompt}
    ]

    print(f"Starting custom agent loop with model {model} (DRY_RUN={DRY_RUN})...", file=sys.stderr)

    loop_count = 0
    max_loops = 30
    
    while loop_count < max_loops:
        loop_count += 1
        
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto"
            )
        except Exception as e:
            print(f"API Error: {e}", file=sys.stderr)
            sys.exit(1)

        msg = response.choices[0].message
        messages.append(msg)

        if not msg.tool_calls:
            # Model responded with text instead of finishing. Just prompt it to finish.
            messages.append({"role": "user", "content": "Please continue and use the finish tool when done."})
            continue

        for tool_call in msg.tool_calls:
            func_name = tool_call.function.name
            
            try:
                args = json.loads(tool_call.function.arguments)
            except Exception as e:
                result = f"Error parsing JSON arguments: {e}"
                messages.append({"role": "tool", "tool_call_id": tool_call.id, "name": func_name, "content": result})
                continue
                
            print(f">> Tool Call: {func_name}", file=sys.stderr)
            
            if func_name == "finish":
                print(json.dumps(args))
                return
                
            elif func_name == "read_file":
                result = read_file(args.get("path", ""))
            elif func_name == "glob_search":
                result = glob_search(args.get("pattern", ""))
            elif func_name == "grep_search":
                result = grep_search(args.get("query", ""))
            elif func_name == "write_file":
                result = write_file(args.get("path", ""), args.get("content", ""))
            elif func_name == "edit_file":
                result = edit_file(args.get("path", ""), args.get("old_string", ""), args.get("new_string", ""))
            else:
                result = f"Unknown function {func_name}"
                
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "name": func_name,
                "content": str(result)
            })

    print(json.dumps({"error": "Exceeded maximum tool call loops"}))
    sys.exit(1)

if __name__ == "__main__":
    main()
