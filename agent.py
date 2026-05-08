#!/usr/bin/env python3
import json, os, subprocess, sys, textwrap, traceback, shutil, time, datetime
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.syntax import Syntax
from rich.markdown import Markdown
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich.layout import Layout
from rich.text import Text
from rich.align import Align
from rich.columns import Columns
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style as PTStyle
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.shortcuts import confirm

try:
    import requests as req_lib
except ImportError:
    print("pip install requests prompt_toolkit rich")
    sys.exit(1)

API_KEY = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")
BASE_URL = os.environ.get("API_BASE_URL", "https://openrouter.ai/api")
MODEL = os.environ.get("MODEL", "deepseek/deepseek-v4-flash")

console = Console(highlight=False)
interrupted = False
show_command_palette = False

total_prompt_tokens = 0
total_completion_tokens = 0

# ASCII Art Logo
LOGO = """
 █████╗  ██████╗ ███████╗███╗   ██╗████████╗
██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝
███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║   
██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║   
██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║   
╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝   
"""

# Command palette options
COMMANDS = [
    "/exit - Exit the agent",
    "/clear - Clear conversation history", 
    "/help - Show help information",
    "/tokens - Show token usage",
    "/model - Show model information",
    "/files - List current directory files",
    "/pwd - Show current working directory",
    "/history - Show command history"
]

SYSTEM_PROMPT = textwrap.dedent("""\
    You are an AI coding agent in a terminal. You have these tools:

    - read(path) - read a file
    - write(path, content) - create a new file
    - edit(path, old_string, new_string) - search-and-replace in existing file
    - bash(command) - run a shell command
    - list_dir(path) - list directory contents
    - grep(pattern, include) - search for regex in files

    Rules:
    1. Read files before editing them
    2. When editing existing files, use edit() not write()
    3. After editing, run a command to verify the result
    4. Think step by step before using tools
    5. Show your chain of thought / reasoning step by step""")

TOOL_DEFS = [
    {"type": "function", "function": {
        "name": "read",
        "description": "Read a file from the filesystem",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "write",
        "description": "Write content to a new file (will overwrite existing!)",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "edit",
        "description": "Edit an existing file via search-and-replace. The old_string must match exactly.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}}, "required": ["path", "old_string", "new_string"]},
    }},
    {"type": "function", "function": {
        "name": "bash",
        "description": "Execute a bash command on the system",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List files and directories in a path",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    }},
    {"type": "function", "function": {
        "name": "grep",
        "description": "Search for a regex pattern recursively in files",
        "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "include": {"type": "string"}}},
    }},
]

MAX_READ_BYTES = 50000
MAX_MESSAGE_TURNS = 30

pt_style = PTStyle.from_dict({
    "prompt": "bold #00d7ff",
    "toolbar": "bg:#1e1e1e #ffffff",
    "status": "bg:#005f87 #ffffff bold",
    "command": "bg:#d75f00 #ffffff bold",
    "logo": "#00d7ff bold",
    "success": "#5faf00",
    "warning": "#ffaf00", 
    "error": "#d70000",
    "info": "#5f87d7",
    "dim": "#808080"
})

kb = KeyBindings()


@kb.add("c-c")
def _(event):
    """Cancel current operation instead of exiting the app"""
    global interrupted
    interrupted = True


@kb.add("c-p")
def _(event):
    """Show command palette"""
    from prompt_toolkit.shortcuts import radiolist_dialog
    try:
        result = radiolist_dialog(
            title="Command Palette",
            text="Select a command:",
            values=[(cmd.split(" - ")[0], cmd) for cmd in COMMANDS]
        ).run()
        if result:
            event.current_buffer.text = result
    except Exception:
        pass


@kb.add("tab")
def _(event):
    """Tab completion for commands and file paths"""
    buffer = event.current_buffer
    text = buffer.text
    
    if text.startswith("/"):
        # Command completion
        commands = ["/exit", "/clear", "/help", "/tokens", "/model", "/files", "/pwd", "/history"]
        matches = [cmd for cmd in commands if cmd.startswith(text)]
        if len(matches) == 1:
            buffer.text = matches[0]
            buffer.cursor_position = len(matches[0])
    else:
        # File path completion (basic)
        words = text.split()
        if words:
            last_word = words[-1]
            if "/" in last_word or "." in last_word:
                try:
                    path_part = Path(last_word).parent if "/" in last_word else Path(".")
                    if path_part.exists():
                        matches = [str(p) for p in path_part.iterdir() if str(p).startswith(last_word)]
                        if len(matches) == 1:
                            words[-1] = matches[0]
                            buffer.text = " ".join(words)
                            buffer.cursor_position = len(buffer.text)
                except:
                    pass


def trim_messages(messages):
    if len(messages) <= 2:
        return messages
    system = messages[0]
    rest = messages[1:]
    if len(rest) > MAX_MESSAGE_TURNS * 2 + 20:
        rest = rest[-(MAX_MESSAGE_TURNS * 2 + 20):]
    return [system] + rest


def get_status_bar():
    """Create top status bar with model info and stats"""
    model_short = MODEL.split("/")[-1] if "/" in MODEL else MODEL
    current_time = datetime.datetime.now().strftime("%H:%M:%S")
    
    status_items = [
        f"🤖 {model_short}",
        f"🕒 {current_time}",
        f"📊 {total_prompt_tokens}→{total_completion_tokens}",
        f"💾 {os.getcwd().split('/')[-1]}"
    ]
    
    return " │ ".join(status_items)


def get_command_bar():
    """Create bottom command bar with shortcuts"""
    shortcuts = [
        "Tab: Complete",
        "Ctrl+P: Commands", 
        "Ctrl+C: Cancel",
        "Enter: Submit",
        "/help: Help"
    ]
    return " │ ".join(shortcuts)


def get_toolbar():
    """Enhanced toolbar with status and command info"""
    return HTML(f'<style bg="#1e1e1e" fg="#ffffff">{get_command_bar()}</style>')


def display_logo():
    """Display ASCII art logo with colors"""
    logo_text = Text(LOGO, style="bold cyan")
    console.print(Align.center(logo_text))
    
    subtitle = Text("AI Coding Agent with Modern TUI", style="dim italic")
    console.print(Align.center(subtitle))
    console.print()


def create_status_panel():
    """Create status panel for display"""
    status_text = get_status_bar()
    return Panel(
        status_text,
        style="bold white on blue",
        height=3,
        padding=(0, 1)
    )


def get_file_language(path):
    """Detect programming language from file extension"""
    ext = Path(path).suffix.lower()
    lang_map = {
        '.py': 'python',
        '.js': 'javascript', 
        '.ts': 'typescript',
        '.jsx': 'jsx',
        '.tsx': 'tsx',
        '.go': 'go',
        '.rs': 'rust',
        '.java': 'java',
        '.cpp': 'cpp',
        '.c': 'c',
        '.h': 'c',
        '.hpp': 'cpp',
        '.cs': 'csharp',
        '.php': 'php',
        '.rb': 'ruby',
        '.sh': 'bash',
        '.sql': 'sql',
        '.html': 'html',
        '.css': 'css',
        '.scss': 'scss',
        '.json': 'json',
        '.xml': 'xml',
        '.yaml': 'yaml',
        '.yml': 'yaml',
        '.md': 'markdown',
        '.dockerfile': 'dockerfile',
        '.toml': 'toml',
        '.ini': 'ini',
        '.cfg': 'ini'
    }
    return lang_map.get(ext, 'text')


def call_llm_stream(messages):
    global total_prompt_tokens, total_completion_tokens
    for attempt in range(3):
        try:
            r = req_lib.post(
                f"{BASE_URL}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/agent-cli",
                    "X-Title": "agent-cli",
                },
                json={
                    "model": MODEL,
                    "messages": messages,
                    "tools": TOOL_DEFS,
                    "tool_choice": "auto",
                    "max_tokens": 16384,
                    "stream": True,
                },
                stream=True,
                timeout=180,
            )
            r.raise_for_status()
            break
        except req_lib.exceptions.RequestException as e:
            if attempt < 2:
                wait = 2 ** attempt
                console.print(f"[yellow]\u26a0\ufe0f  API error: {e}, retrying in {wait}s...[/yellow]")
                time.sleep(wait)
            else:
                raise

    content = ""
    tool_calls = {}
    usage_chunk = None
    role = None

    def make_tool_call(index):
        tc = tool_calls.setdefault(index, {})
        tc.setdefault("id", None)
        tc.setdefault("type", "function")
        tc.setdefault("function", {"name": "", "arguments": ""})
        return tc

    for line in r.iter_lines():
        if not line:
            continue
        if line.startswith(b"data: "):
            payload = line[6:]
        elif line.startswith(b"data:") and len(line) > 5:
            payload = line[5:]
        else:
            continue
        if payload.strip() == b"[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue

        delta = chunk.get("choices", [{}])[0].get("delta", {})
        finish_reason = chunk.get("choices", [{}])[0].get("finish_reason")

        if not role and delta.get("role"):
            role = delta["role"]

        if delta.get("content"):
            content += delta["content"]
            yield content, None, None

        for tc_delta in delta.get("tool_calls", []):
            idx = tc_delta.get("index", 0)
            tc = make_tool_call(idx)
            if "id" in tc_delta and tc_delta["id"] is not None:
                tc["id"] = tc_delta["id"]
            if tc_delta.get("function"):
                fn = tc_delta["function"]
                if "name" in fn and fn["name"] is not None:
                    tc["function"]["name"] += fn["name"]
                if "arguments" in fn and fn["arguments"] is not None:
                    tc["function"]["arguments"] += fn["arguments"]

        # Token usage from last chunk with finish_reason
        if finish_reason and not usage_chunk:
            usage_info = chunk.get("usage")
            if usage_info:
                usage_chunk = usage_info

    usage = usage_chunk or r.headers.get("X-Usage") or r.headers.get("X-Token-Usage")
    if isinstance(usage, dict):
        total_prompt_tokens += usage.get("prompt_tokens", 0)
        total_completion_tokens += usage.get("completion_tokens", 0)
    elif isinstance(usage, str):
        try:
            u = json.loads(usage)
            total_prompt_tokens += u.get("prompt_tokens", 0)
            total_completion_tokens += u.get("completion_tokens", 0)
        except (json.JSONDecodeError, TypeError):
            pass

    final_tool_calls = None
    if tool_calls:
        sorted_indices = sorted(tool_calls.keys())
        final_tool_calls = []
        for idx in sorted_indices:
            tc = tool_calls[idx]
            if tc["id"] and tc["function"]["name"]:
                final_tool_calls.append({
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    },
                })

    message = {"role": role or "assistant"}
    if content:
        message["content"] = content
    if final_tool_calls:
        message["tool_calls"] = final_tool_calls

    yield content, message, finish_reason



def execute_tool(name, args):
    if name == "read":
        path = args["path"]
        try:
            data = Path(path).read_bytes()
            text = data.decode("utf-8", errors="replace")
            label = f"Read {path} ({len(data)} bytes)"
            if len(text) > MAX_READ_BYTES:
                text = text[:MAX_READ_BYTES] + f"\n... (truncated at {MAX_READ_BYTES} bytes)"
            return text, label
        except FileNotFoundError:
            err = f"Error: file not found: {path}"
            return err, err
        except Exception as e:
            err = f"Error reading {path}: {e}"
            return err, err

    if name == "write":
        path = args["path"]
        if Path(path).exists():
            console.print(f"[yellow]\u26a0\ufe0f  Overwriting existing file: {path}[/yellow]")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(args["content"])
        msg = f"Written {len(args['content'])} bytes to {path}"
        return msg, msg

    if name == "edit":
        path = args["path"]
        old, new = args["old_string"], args["new_string"]
        try:
            content = Path(path).read_text()
        except FileNotFoundError:
            err = f"Error: file not found: {path}"
            return err, err
        if old not in content:
            err = f"Error: old_string not found in {path}"
            return err, err
        count = content.count(old)
        if count > 1:
            err = f"Error: old_string appears {count} times in {path}, be more specific"
            return err, err
        Path(path).write_text(content.replace(old, new, 1))
        msg = f"Edited {path} (1 replacement)"
        return msg, msg

    if name == "bash":
        cmd = args["command"]
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
            out = r.stdout or ""
            err = r.stderr or ""
            model_out = ""
            if out:
                model_out += out
            MAX_BASH_OUTPUT = 100000
            if len(model_out) > MAX_BASH_OUTPUT:
                model_out = model_out[:MAX_BASH_OUTPUT] + f"\n... (truncated at {MAX_BASH_OUTPUT} chars)"
            if err:
                if model_out:
                    model_out += "\n"
                model_out += err
            if r.returncode != 0:
                if model_out:
                    model_out += "\n"
                model_out += f"(exit code: {r.returncode})"
            if not model_out:
                model_out = "(no output)"
            display = f"$ {cmd}"
            return model_out, display
        except subprocess.TimeoutExpired:
            return "Error: command timed out (120s)", "Error: command timed out"
        except Exception as e:
            return f"Error: {e}", f"Error: {e}"

    if name == "list_dir":
        path = args.get("path", ".")
        try:
            items = sorted(Path(path).iterdir(), key=lambda x: (x.is_file(), x.name))
            lines = ["Contents of " + path + ":"]
            for item in items:
                suffix = "/" if item.is_dir() else ""
                lines.append(f"  {item.name}{suffix}")
            result = "\n".join(lines)
            return result, result
        except Exception as e:
            err = f"Error: {e}"
            return err, err

    if name == "grep":
        pattern = args["pattern"]
        include = args.get("include", "*.py,*.js,*.ts,*.go,*.rs,*.md")
        try:
            import fnmatch, re
            inc_patterns = [p.strip() for p in include.split(",")]
            matches = []
            for fpath in Path(".").rglob("*"):
                if not fpath.is_file():
                    continue
                if not any(fnmatch.fnmatch(fpath.name, p) for p in inc_patterns):
                    continue
                try:
                    for lineno, line in enumerate(fpath.read_text(errors="replace").splitlines(), 1):
                        try:
                            if re.search(pattern, line):
                                matches.append(f"{fpath}:{lineno}:{line}")
                        except re.error:
                            if pattern in line:
                                matches.append(f"{fpath}:{lineno}:{line}")
                except (OSError, ValueError):
                    pass
            result = "\n".join(matches) if matches else "No matches found"
            return result, f"grep '{pattern}'"
        except Exception as e:
            err = f"Error: {e}"
            return err, err

    err = f"Unknown tool: {name}"
    return err, err


def display_tool_result(name, args, result):
    """Enhanced tool result display with better formatting and colors"""
    if name == "bash" and result:
        if result == "(no output)":
            console.print(Panel(
                f"[dim]Command completed with no output[/dim]",
                title=f"[bold cyan]💻 $ {args['command']}[/bold cyan]",
                border_style="cyan",
                padding=(1, 2)
            ))
        else:
            # Enhanced bash output with syntax highlighting
            console.print(Panel(
                Syntax(result, "bash", theme="monokai", line_numbers=False),
                title=f"[bold cyan]💻 $ {args['command']}[/bold cyan]",
                border_style="cyan",
                padding=(1, 2)
            ))
    elif name == "read" and result and "Error" not in result:
        # Enhanced file reading with syntax highlighting
        path = args['path']
        language = get_file_language(path)

        max_preview = MAX_READ_BYTES
        preview = result[:max_preview]
        if len(result) > max_preview:
            preview += "\n... (truncated)"

        console.print(Panel(
            Syntax(preview, language, theme="monokai", line_numbers=True),
            title=f"[bold green]📄 {path}[/bold green] ({len(result)} bytes)",
            border_style="green",
            padding=(1, 2)
        ))
    elif name == "write":
        # Enhanced write confirmation
        path = Path(args['path'])
        size = len(args['content'])
        console.print(Panel(
            f"[green]✅ Successfully written {size} bytes[/green]",
            title=f"[bold blue]💾 {path.name}[/bold blue]",
            border_style="blue"
        ))
    elif name == "edit":
        # Enhanced edit confirmation  
        path = args['path']
        console.print(Panel(
            f"[green]✅ File edited successfully[/green]",
            title=f"[bold yellow]✏️  {Path(path).name}[/bold yellow]",
            border_style="yellow"
        ))
    elif name == "list_dir":
        # Enhanced directory listing
        if "Error" not in result:
            lines = result.split('\n')[1:]  # Skip header
            if lines:
                table = Table(show_header=True, header_style="bold magenta")
                table.add_column("📁 Name", style="cyan")
                table.add_column("Type", style="dim")
                
                for line in lines:
                    if line.strip():
                        name = line.strip()
                        if name.endswith('/'):
                            table.add_row(name[:-1], "Directory")
                        else:
                            table.add_row(name, "File")
                            
                console.print(Panel(
                    table,
                    title=f"[bold magenta]📂 {args.get('path', '.')}[/bold magenta]",
                    border_style="magenta"
                ))
        else:
            console.print(f"[red]❌ {result}[/red]")
    elif name == "grep":
        # Enhanced grep results
        if "No matches found" not in result and "Error" not in result:
            console.print(Panel(
                Syntax(result, "text", theme="monokai"),
                title=f"[bold green]🔍 grep '{args['pattern']}'[/bold green]",
                border_style="green"
            ))
        else:
            console.print(f"[yellow]⚠️  {result}[/yellow]")
    elif "Error" in (result or ""):
        # Enhanced error display
        console.print(Panel(
            f"[red]{result}[/red]",
            title="[bold red]❌ Error[/bold red]",
            border_style="red"
        ))


def main():
    global total_prompt_tokens, total_completion_tokens, interrupted

    if not API_KEY:
        console.print("[red]❌ Set OPENROUTER_API_KEY or OPENAI_API_KEY[/red]")
        sys.exit(1)

    width = min(shutil.get_terminal_size().columns, 120)
    
    # Clear screen and display logo
    console.clear()
    display_logo()
    
    # Display status panel
    console.print(create_status_panel())
    console.print()

    # Welcome message with enhanced styling
    welcome_panel = Panel(
        "[bold cyan]Welcome to the AI Coding Agent![/bold cyan]\n\n"
        "[dim]Available commands:[/dim]\n"
        "• [green]/help[/green] - Show help information\n"
        "• [green]/clear[/green] - Clear conversation history\n"
        "• [green]/tokens[/green] - Show token usage\n"
        "• [green]/model[/green] - Show model information\n"
        "• [green]/exit[/green] - Exit the agent\n\n"
        "[dim]Shortcuts:[/dim]\n"
        "• [yellow]Tab[/yellow] - Auto-complete commands and paths\n"
        "• [yellow]Ctrl+P[/yellow] - Open command palette\n"
        "• [yellow]Ctrl+C[/yellow] - Cancel current operation\n"
        "• [yellow]Enter[/yellow] - Submit input",
        title="[bold blue]🚀 Getting Started[/bold blue]",
        border_style="blue",
        width=width
    )
    console.print(welcome_panel)
    console.print()

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    session = PromptSession(
        history=FileHistory(str(Path.home() / ".agent_history")),
        style=pt_style,
        key_bindings=kb,
        multiline=False,
        bottom_toolbar=get_toolbar,
    )

    while True:
        interrupted = False
        try:
            prompt = session.prompt(">>> ")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]👋 Goodbye![/yellow]")
            break

        prompt = prompt.strip()
        if not prompt:
            continue
            
        # Handle special commands
        if prompt == "/exit":
            console.print("[yellow]👋 Goodbye![/yellow]")
            break
        elif prompt == "/clear":
            messages = [messages[0]]
            total_prompt_tokens = 0
            total_completion_tokens = 0
            console.clear()
            display_logo()
            console.print("[dim]✨ Context cleared[/dim]\n")
            continue
        elif prompt == "/help":
            help_panel = Panel(
                f"[bold]🤖 Model:[/bold] {MODEL}\n"
                f"[bold]🌐 API Base:[/bold] {BASE_URL}\n\n"
                f"[bold]📋 Commands:[/bold]\n"
                f"• [green]/exit[/green] - Exit the agent\n"
                f"• [green]/clear[/green] - Clear conversation history\n"
                f"• [green]/help[/green] - Show this help\n"
                f"• [green]/tokens[/green] - Show token usage\n"
                f"• [green]/model[/green] - Show model info\n"
                f"• [green]/files[/green] - List current directory\n"
                f"• [green]/pwd[/green] - Show current directory\n\n"
                f"[bold]⌨️  Shortcuts:[/bold]\n"
                f"• [yellow]Enter[/yellow] - Submit input\n"
                f"• [yellow]Tab[/yellow] - Auto-complete\n"
                f"• [yellow]Ctrl+P[/yellow] - Command palette\n"
                f"• [yellow]Ctrl+C[/yellow] - Cancel\n\n"
                f"[bold]💡 Tip:[/bold] Set MODEL=deepseek/deepseek-r1 for reasoning",
                title="[bold blue]📖 Help[/bold blue]",
                border_style="blue"
            )
            console.print(help_panel)
            console.print()
            continue
        elif prompt == "/tokens":
            token_panel = Panel(
                f"[bold green]📊 Token Usage Statistics[/bold green]\n\n"
                f"• [cyan]Prompt tokens:[/cyan] {total_prompt_tokens:,}\n"
                f"• [cyan]Completion tokens:[/cyan] {total_completion_tokens:,}\n"
                f"• [cyan]Total tokens:[/cyan] {total_prompt_tokens + total_completion_tokens:,}",
                title="[bold green]📈 Usage[/bold green]",
                border_style="green"
            )
            console.print(token_panel)
            console.print()
            continue
        elif prompt == "/model":
            model_panel = Panel(
                f"[bold cyan]🤖 Model Information[/bold cyan]\n\n"
                f"• [yellow]Model:[/yellow] {MODEL}\n"
                f"• [yellow]API Base:[/yellow] {BASE_URL}\n"
                f"• [yellow]Max tokens:[/yellow] 16,384\n"
                f"• [yellow]Timeout:[/yellow] 180s",
                title="[bold cyan]⚙️  Configuration[/bold cyan]",
                border_style="cyan"
            )
            console.print(model_panel)
            console.print()
            continue
        elif prompt == "/files":
            try:
                items = sorted(Path(".").iterdir(), key=lambda x: (x.is_file(), x.name))
                table = Table(show_header=True, header_style="bold magenta")
                table.add_column("📁 Name", style="cyan")
                table.add_column("Type", style="dim")
                table.add_column("Size", style="yellow")
                
                for item in items:
                    if item.is_dir():
                        table.add_row(item.name, "Directory", "-")
                    else:
                        size = item.stat().st_size
                        size_str = f"{size:,} bytes" if size < 1024 else f"{size/1024:.1f} KB"
                        table.add_row(item.name, "File", size_str)
                        
                console.print(Panel(
                    table,
                    title="[bold magenta]📂 Current Directory[/bold magenta]",
                    border_style="magenta"
                ))
            except Exception as e:
                console.print(f"[red]❌ Error listing files: {e}[/red]")
            console.print()
            continue
        elif prompt == "/pwd":
            pwd_panel = Panel(
                f"[bold green]📍 Current Working Directory[/bold green]\n\n"
                f"[cyan]{os.getcwd()}[/cyan]",
                title="[bold green]📂 Location[/bold green]",
                border_style="green"
            )
            console.print(pwd_panel)
            console.print()
            continue

        messages = trim_messages(messages)
        messages.append({"role": "user", "content": prompt})

        while True:
            if interrupted:
                interrupted = False
                break

            try:
                content_so_far = ""
                full_message = None

                live = Live(console=console, refresh_per_second=12, transient=False)
                live.start()
                try:
                    for content_so_far, msg, finish in call_llm_stream(messages):
                        if interrupted:
                            break
                        if content_so_far:
                            live.update(Markdown(content_so_far))
                        if msg is not None:
                            full_message = msg
                finally:
                    live.stop()
                    console.print()

                if interrupted:
                    break

                tool_calls = (full_message or {}).get("tool_calls", [])

                if tool_calls:
                    messages.append(full_message)
                    for tc in tool_calls:
                        if interrupted:
                            break
                        fn = tc["function"]
                        name = fn["name"]
                        try:
                            args = json.loads(fn["arguments"])
                        except json.JSONDecodeError:
                            console.print("[red]\u274c Invalid JSON in tool arguments, skipping[/red]")
                            continue

                        # Enhanced tool execution display
                        tool_label = f"🔧 [{name}]"
                        if name == "bash":
                            tool_label += f" $ {args['command']}"
                        elif name in ("read", "write", "edit"):
                            tool_label += f" {args['path']}"
                        elif name == "list_dir":
                            tool_label += f" {args.get('path', '.')}"
                        elif name == "grep":
                            tool_label += f" '{args['pattern']}'"
                            
                        console.print(f"[bold blue]{tool_label}[/bold blue]")

                        with Progress(
                            SpinnerColumn(),
                            TextColumn("[progress.description]{task.description}"),
                            console=console,
                            transient=True,
                        ) as progress:
                            progress.add_task(f"Executing {name}...", total=None)
                            result, display = execute_tool(name, args)

                        display_tool_result(name, args, result)
                        console.print()

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result,
                        })
                    if interrupted:
                        break
                else:
                    if full_message:
                        messages.append(full_message)
                    break

            except KeyboardInterrupt:
                interrupted = True
                console.print("\n[yellow]⚠️  [interrupted][/yellow]")
                break
            except Exception as e:
                console.print(Panel(
                    traceback.format_exc(),
                    title="[red]❌ Error[/red]",
                    border_style="red",
                ))
                break


if __name__ == "__main__":
    main()