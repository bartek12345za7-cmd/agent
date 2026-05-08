# Autonomous AI Agent 🤖

A streamlined, single-file autonomous AI coding agent built with Python. It interacts directly with your local file system and shell to help you write, debug, and manage code from your terminal.

> **Note**: This is an **experimental/non-production** project designed for rapid prototyping and educational purposes.

---

## 🚀 Features

* **Autonomous Tool Use**: The agent can read, write, and edit files, as well as execute bash commands independently to accomplish complex tasks.
* **Smart File Editing**: Uses exact search-and-replace logic for precise file modifications rather than overwriting entire files.
* **Token & Context Management**: Includes built-in token counting and automatic message trimming to maintain long-running conversations within context limits.
* **Single-File Architecture**: No complex installation or directory structures required—the entire core logic is contained within `agent.py`.

## 🛠️ Integrated Tools

The agent is equipped with a specialized toolbox to interact with your environment:
* `bash(command)`: Execute shell commands (includes a 120s timeout and output truncation for safety).
* `read(path)`: Read file contents.
* `write(path, content)`: Create new files or overwrite existing ones.
* `edit(path, old, new)`: Precise string replacement in existing code.
* `grep(pattern)`: Search for regex patterns across your project.
* `list_dir(path)`: Explore your directory structure.

---

## 🔧 Installation & Setup

1. **Download the script**:
   Clone the repository or simply download the `agent.py` file to your working directory.

2. **Install required dependencies**:
   The script requires a few specific Python libraries to run. Install them using `pip`:
   ```bash
   pip install requests prompt_toolkit rich
3. In the terminal, export your API key and run the script:

```bash
export OPENROUTER_API_KEY="your-openrouter-api-key"
python3 agent.py
```
and it's all!
