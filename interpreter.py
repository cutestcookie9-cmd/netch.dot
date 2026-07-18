import sys
import re
import os
import shutil
import subprocess
import platform
import time
import textwrap
import tempfile
import json
import urllib.request
import datetime

GITHUB_REPO = "cutestcookie9-cmd/netch.dot"
GITHUB_BRANCH = "main"
GITHUB_FILE_PATH = "interpreter.py"

try:
    import tkinter as tk
except ImportError:
    tk = None


def get_indent(line):
    stripped_line = line.lstrip(' \t')
    return len(line) - len(stripped_line)


def peek_next_indent(lines, i):
    j = i
    while j < len(lines) and lines[j].strip() == '':
        j += 1
    if j >= len(lines):
        return None
    return get_indent(lines[j])


def parse_block(lines, i, base_indent):
    """Turns raw indented lines into a nested list of statements.
    Each statement is a dict: {"type": "line"/"if"/"funcdef", ...}
    This is what lets netch understand 'blocks' of code (if-bodies, function bodies)."""
    statements = []
    while i < len(lines):
        if lines[i].strip() == '':
            i += 1
            continue
        indent = get_indent(lines[i])
        if indent < base_indent:
            break
        stripped = lines[i].strip()

        func_match = re.match(r'^function\s+(\w+)\s*\((.*?)\)\s*:$', stripped)
        if func_match:
            fname, param_str = func_match.groups()
            params = [p.strip() for p in param_str.split(',') if p.strip()]
            child_indent = peek_next_indent(lines, i + 1)
            if child_indent is None or child_indent <= indent:
                body, i = [], i + 1
            else:
                body, i = parse_block(lines, i + 1, child_indent)
            statements.append({"type": "funcdef", "name": fname, "params": params, "body": body})
            continue

        while_match = re.match(r'^while\s+(.+):$', stripped)
        if while_match:
            condition = while_match.group(1).strip()
            child_indent = peek_next_indent(lines, i + 1)
            if child_indent is None or child_indent <= indent:
                body, i = [], i + 1
            else:
                body, i = parse_block(lines, i + 1, child_indent)
            statements.append({"type": "while", "condition": condition, "body": body})
            continue

        foreach_match = re.match(r'^for each\s+(\w+)\s+in\s+(\w+)\s*:$', stripped)
        if foreach_match:
            item_name, list_name = foreach_match.groups()
            child_indent = peek_next_indent(lines, i + 1)
            if child_indent is None or child_indent <= indent:
                body, i = [], i + 1
            else:
                body, i = parse_block(lines, i + 1, child_indent)
            statements.append({"type": "foreach", "item_name": item_name, "list_name": list_name, "body": body})
            continue

        repeat_match = re.match(r'^repeat\s+(.+)\s+times:$', stripped)
        if repeat_match:
            count_expr = repeat_match.group(1).strip()
            child_indent = peek_next_indent(lines, i + 1)
            if child_indent is None or child_indent <= indent:
                body, i = [], i + 1
            else:
                body, i = parse_block(lines, i + 1, child_indent)
            statements.append({"type": "repeat", "count_expr": count_expr, "body": body})
            continue

        if_match = re.match(r'^if\s+(.+):$', stripped)
        if if_match:
            condition = if_match.group(1).strip()
            child_indent = peek_next_indent(lines, i + 1)
            if child_indent is None or child_indent <= indent:
                body, i = [], i + 1
            else:
                body, i = parse_block(lines, i + 1, child_indent)

            else_body = []
            j = i
            while j < len(lines) and lines[j].strip() == '':
                j += 1
            if j < len(lines) and get_indent(lines[j]) == indent and lines[j].strip() == 'else:':
                child_indent2 = peek_next_indent(lines, j + 1)
                if child_indent2 is None or child_indent2 <= indent:
                    else_body, i = [], j + 1
                else:
                    else_body, i = parse_block(lines, j + 1, child_indent2)

            statements.append({"type": "if", "condition": condition, "body": body, "else_body": else_body})
            continue

        if stripped == '<python>':
            j = i + 1
            code_lines = []
            while j < len(lines) and lines[j].strip() != '</python>':
                code_lines.append(lines[j])
                j += 1
            code = textwrap.dedent("\n".join(code_lines))
            statements.append({"type": "pyblock", "code": code})
            i = j + 1  # skips past the </python> line too (or end of file if never closed — already validated earlier)
            continue

        if stripped == '<bat>':
            j = i + 1
            code_lines = []
            while j < len(lines) and lines[j].strip() != '</bat>':
                code_lines.append(lines[j])
                j += 1
            code = textwrap.dedent("\n".join(code_lines))
            statements.append({"type": "batblock", "code": code})
            i = j + 1
            continue

        statements.append({"type": "line", "text": stripped})
        i += 1

    return statements, i


def collect_functions(statements, out):
    for s in statements:
        if s["type"] == "funcdef":
            out[s["name"]] = {"params": s["params"], "body": s["body"]}
            collect_functions(s["body"], out)
        elif s["type"] == "if":
            collect_functions(s["body"], out)
            collect_functions(s["else_body"], out)
        elif s["type"] in ("while", "repeat", "foreach"):
            collect_functions(s["body"], out)


def check_python_blocks(raw_lines):
    """Enforces netch's rule for mixing in raw Python or .bat commands:
    - every <python>/<bat> block needs a '# why: ...' comment right above it explaining the reason
    - raw Python + bat combined can never be more than 30% of the file (netch-majority rule)
    Returns True if the file is OK to run, False if it should stop."""
    total_code_lines = 0
    foreign_lines = 0
    i = 0
    while i < len(raw_lines):
        stripped = raw_lines[i].strip()
        if stripped == '':
            i += 1
            continue
        if stripped in ('<python>', '<bat>'):
            tag = 'python' if stripped == '<python>' else 'bat'
            close_tag = f'</{tag}>'
            # find the justification comment on the nearest preceding non-blank line
            j = i - 1
            while j >= 0 and raw_lines[j].strip() == '':
                j -= 1
            has_reason = j >= 0 and re.match(r'^#\s*why\s*:\s*.+', raw_lines[j].strip(), re.IGNORECASE)
            if not has_reason:
                print(f"[netch warning] the <{tag}> block at line {i + 1} is missing a '# why: ...' comment "
                      f"right above it explaining why you need raw {tag} here instead of netch. "
                      f"Continue anyway? (y = continue / n = stop)")
                choice = input("> ").strip().lower()
                if choice != 'y':
                    print("[netch] stopped.")
                    return False

            k = i + 1
            block_lines = 0
            closed = False
            while k < len(raw_lines):
                if raw_lines[k].strip() == close_tag:
                    closed = True
                    break
                if raw_lines[k].strip() != '':
                    block_lines += 1
                k += 1
            if not closed:
                print(f"[netch error] the <{tag}> block starting at line {i + 1} is missing its closing {close_tag} tag.")
                return False

            foreign_lines += block_lines
            total_code_lines += block_lines
            i = k + 1
            continue

        total_code_lines += 1
        i += 1

    if total_code_lines > 0 and (foreign_lines / total_code_lines) > 0.30:
        percent = round((foreign_lines / total_code_lines) * 100)
        print(f"[netch warning] this file is {percent}% raw Python/bat — netch scripts have to stay mostly netch "
              f"(raw Python + bat combined can only make up under 30% of the file). Move some of that logic into "
              f"real netch, or trim down the <python>/<bat> blocks, then run it again.")
        return False

    return True


def get_version_file_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "version.txt")


def read_local_version():
    path = get_version_file_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return None


def write_local_version(date_str):
    try:
        with open(get_version_file_path(), "w") as f:
            f.write(date_str)
    except Exception:
        pass


def get_remote_commit_date():
    """Asks GitHub when interpreter.py was last committed on the main branch. Returns an ISO date string, or None if it can't be reached."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/commits?path={GITHUB_FILE_PATH}&sha={GITHUB_BRANCH}&per_page=1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "netch-updater"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not data:
            return None
        return data[0]["commit"]["committer"]["date"]
    except Exception:
        return None


def download_latest_interpreter():
    url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/{GITHUB_FILE_PATH}"
    dest = os.path.abspath(__file__)
    req = urllib.request.Request(url, headers={"User-Agent": "netch-updater"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        new_code = resp.read()
    with open(dest, "wb") as f:
        f.write(new_code)


def check_for_update():
    """Compares the interpreter.py version this file was installed with against what's currently on GitHub.
    Returns True if it's safe to keep running, False if the script should stop."""
    local_date_str = read_local_version()
    if not local_date_str:
        return True  # no version on record (e.g. running the raw file directly) — don't block

    remote_date_str = get_remote_commit_date()
    if not remote_date_str:
        return True  # couldn't reach GitHub (no internet, etc.) — don't block on a network hiccup

    try:
        local_date = datetime.datetime.fromisoformat(local_date_str.replace("Z", "+00:00"))
        remote_date = datetime.datetime.fromisoformat(remote_date_str.replace("Z", "+00:00"))
    except Exception:
        return True  # couldn't parse either date — don't block

    if remote_date <= local_date:
        return True  # up to date

    print("NEW NETCH VERSION DETECTED IF YOU DO NOT DOWNLOAD NETCH CAN BREAK AND GET SO MANY ERRORS "
          "MEANING NEWER NETCH SCRIPTS WILL NOT WORK WOULD YOU LIKE TO AUTO DOWNLOAD IT RIGHT NOW?")
    choice = input("(y/n) > ").strip().lower()
    if choice != 'y':
        print("[netch] not updating — stopped so an outdated interpreter doesn't run a script it can't handle.")
        return False

    try:
        download_latest_interpreter()
        write_local_version(remote_date_str)
        print("[netch] updated! Run your file again to use the new version.")
    except Exception as e:
        print(f"[netch error] update failed: {e}")
        print("Try running installer.py again to reinstall.")
    return False


def run_netch(filepath):
    if not check_for_update():
        return False

    with open(filepath, 'r') as f:
        raw_lines = [line.rstrip('\n') for line in f.readlines()]

    if not raw_lines or raw_lines[0].strip() != '<using.netch>':
        print("[netch warning] missing <using.netch> at top of file, running anyway...\n")

    window_mode = len(raw_lines) > 1 and raw_lines[1].strip() == '<window.using>'

    if not check_python_blocks(raw_lines):
        return False

    window_state = {"root": None, "widgets": [], "buttons": {}, "selections": {}, "textboxes": {}, "button_clicked": {},
                     "labels": {}, "box_geometry": {}, "button_geo": {}, "button_scope": {}}
    bot_state = {"token": None, "onmessage_func": None, "prefix": "!", "commands": {}}

    variables = {}
    functions = {}

    program, _ = parse_block(raw_lines, 0, 0)
    collect_functions(program, functions)

    # ---- helper: try to evaluate a math expression, substituting known variables ----
    def try_math(expr, scope):
        tokens = re.findall(r'[A-Za-z_]\w*|\d+\.?\d*|[+\-*/().]', expr)
        if not tokens:
            return None
        rebuilt = ""
        for tok in tokens:
            if re.match(r'^[A-Za-z_]\w*$', tok):
                if tok in scope and isinstance(scope[tok], (int, float)):
                    rebuilt += str(scope[tok])
                else:
                    return None
            else:
                rebuilt += tok
        if not re.search(r'[+\-*/]', rebuilt):
            return None
        try:
            if not re.match(r'^[\d+\-*/(). ]+$', rebuilt):
                return None
            return eval(rebuilt, {"__builtins__": {}}, {})
        except Exception:
            return None

    # ---- helper: split a comma-separated list literal's inside text, respecting quotes and brackets ----
    def split_list_items(inner):
        items = []
        current = ""
        depth = 0
        in_quotes = False
        for ch in inner:
            if ch == '"':
                in_quotes = not in_quotes
                current += ch
            elif ch == '[' and not in_quotes:
                depth += 1
                current += ch
            elif ch == ']' and not in_quotes:
                depth -= 1
                current += ch
            elif ch == ',' and depth == 0 and not in_quotes:
                items.append(current.strip())
                current = ""
            else:
                current += ch
        if current.strip():
            items.append(current.strip())
        return items

    # ---- helper: parse a netch list literal like [1, 2, apple, "banana split"] into a real python list ----
    def parse_list_literal(text, scope):
        text = text.strip()
        if not (text.startswith('[') and text.endswith(']')):
            return None
        inner = text[1:-1].strip()
        if inner == "":
            return []
        return [resolve_value(item, scope) for item in split_list_items(inner)]

    # ---- helper: resolve a single value (string literal, number, variable, list literal, index, or selection.name) ----
    def resolve_value(token, scope):
        token = token.strip()
        if token.startswith('"') and token.endswith('"'):
            return token[1:-1]
        if token.startswith('[') and token.endswith(']'):
            result = parse_list_literal(token, scope)
            if result is not None:
                return result
        sel_match = re.match(r'^selection\.(\w+)$', token)
        if sel_match and sel_match.group(1) in window_state["selections"]:
            return window_state["selections"][sel_match.group(1)]["var"].get()
        if token in window_state["textboxes"]:
            return window_state["textboxes"][token].get("1.0", "end-1c")
        index_match = re.match(r'^(\w+)\[(.+)\]$', token)
        if index_match:
            list_name, index_expr = index_match.groups()
            if list_name in scope and isinstance(scope[list_name], list):
                try:
                    idx = int(resolve_value(index_expr, scope))
                    return scope[list_name][idx]
                except (ValueError, TypeError, IndexError):
                    print(f"[netch error] '{token}' — that index isn't valid for '{list_name}'")
                    return None
        if token in scope:
            return scope[token]
        try:
            return float(token) if '.' in token else int(token)
        except ValueError:
            return token

    # ---- helper: evaluate an if-condition like `chosen == "Pizza"` ----
    def evaluate_condition(cond, scope):
        cond = cond.strip()
        if cond == "true":
            return True
        if cond == "false":
            return False

        clicked_match = re.match(r'^button\.clicked\((\w+)\)$', cond)
        if clicked_match:
            btn_name = clicked_match.group(1)
            if btn_name not in window_state["buttons"]:
                print(f"[netch error] button '{btn_name}' doesn't exist yet")
                return False
            was_clicked = window_state["button_clicked"].get(btn_name, False)
            window_state["button_clicked"][btn_name] = False  # consume the click so it only fires once per press
            return was_clicked

        m = re.match(r'^(.+?)\s*(==|!=|>=|<=|>|<)\s*(.+)$', cond)
        if not m:
            print(f"[netch error] couldn't understand the condition '{cond}' (use ==, !=, >, <, >=, or <=)")
            return False
        left, op, right = m.groups()
        lval = resolve_value(left, scope)
        rval = resolve_value(right, scope)
        try:
            if op == '==':
                return lval == rval
            if op == '!=':
                return lval != rval
            if op == '>':
                return lval > rval
            if op == '<':
                return lval < rval
            if op == '>=':
                return lval >= rval
            if op == '<=':
                return lval <= rval
        except TypeError:
            print(f"[netch error] can't compare '{lval}' and '{rval}'")
            return False
        return False

    # ---- helper: auto-install a python package the first time a feature needs it ----
    def ensure_installed(module_name, pip_name=None):
        pip_name = pip_name or module_name
        try:
            return __import__(module_name)
        except ImportError:
            print(f"[netch] setting up '{pip_name}' for this feature (one-time, first use only)...")
            try:
                subprocess.run([sys.executable, "-m", "pip", "install", pip_name], check=True)
                return __import__(module_name)
            except Exception as e:
                print(f"[netch error] couldn't auto-install '{pip_name}': {e}")
                return None

    # ---- helper: get or create the tkinter window ----
    def draw_rounded_rect(cv, x1, y1, x2, y2, radius, **kwargs):
        radius = max(0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
        points = [
            x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
            x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
            x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
        ]
        return cv.create_polygon(points, smooth=True, **kwargs)

    def render_button(name):
        """(Re)draws a button from its stored geometry — a plain flat tk.Button
        when radius is 0, or a Canvas-drawn rounded shape when button.name.round()
        has been used. Called after any create/color/size/position/round change."""
        geo = window_state["button_geo"][name]
        old_widget = window_state["buttons"].get(name)
        if old_widget:
            old_widget.destroy()

        root = get_window(window_state)
        scope = window_state["button_scope"][name]
        action_text = geo["action_text"]

        def on_click():
            window_state["button_clicked"][name] = True
            if action_text:
                execute([{"type": "line", "text": action_text}], scope)

        if geo["radius"] > 0:
            bg_color = root.cget("bg")
            cv = tk.Canvas(root, width=geo["w"], height=geo["h"], bg=bg_color, highlightthickness=0)
            draw_rounded_rect(cv, 1, 1, geo["w"] - 1, geo["h"] - 1, geo["radius"], fill=geo["color"], outline=geo["color"])
            cv.create_text(geo["w"] / 2, geo["h"] / 2, text=geo["label"], fill="white", font=("Segoe UI", geo["font_size"]))
            cv.bind("<Button-1>", lambda e: on_click())
            cv.config(cursor="hand2")
            cv.place(x=geo["x"], y=geo["y"])
            window_state["buttons"][name] = cv
        else:
            btn = tk.Button(root, text=geo["label"], font=("Segoe UI", geo["font_size"]), bg=geo["color"], fg="white",
                             activebackground=geo["color"], activeforeground="white",
                             relief="flat", borderwidth=0, cursor="hand2", command=on_click)
            btn.place(x=geo["x"], y=geo["y"], width=geo["w"], height=geo["h"])
            window_state["buttons"][name] = btn

    def get_window(state):
        if state["root"] is None:
            if tk is None:
                raise RuntimeError("tkinter isn't available in this environment")
            state["root"] = tk.Tk()
            state["root"].title("netch app")
            state["root"].configure(bg="#f4f5f7")
            state["root"].geometry("500x400")
            state["root"].minsize(250, 150)
            state["closed"] = False

            def on_close():
                state["closed"] = True
                state["root"].destroy()

            state["root"].protocol("WM_DELETE_WINDOW", on_close)
        return state["root"]

    # ---- helper: safely refresh the window each loop tick; returns False once the window has been closed ----
    def refresh_window(state):
        if not state.get("root") or state.get("closed"):
            return False
        try:
            state["root"].update()
            return True
        except tk.TclError:
            state["closed"] = True
            return False

    # ---- helper: start a discord bot using bot_state + functions ----
    def start_bot(scope):
        if not bot_state["token"]:
            print("[netch error] bot.token isn't set")
            return
        discord = ensure_installed("discord", "discord.py")
        if discord is None:
            return

        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready():
            print(f"[netch] bot is online as {client.user}")

        @client.event
        async def on_message(message):
            print(f"[netch debug] event received from {message.author} in #{message.channel}: {message.content!r}")

            if message.author == client.user:
                return

            content = message.content
            if content == "":
                print("[netch warning] received a message but its text was empty — you probably need to turn on "
                      "'MESSAGE CONTENT INTENT' for your bot at https://discord.com/developers/applications "
                      "(Bot tab -> Privileged Gateway Intents), then restart the bot.")
                return

            if content.startswith(bot_state["prefix"]):
                cmd_word = content[len(bot_state["prefix"]):].split(" ")[0]
                if cmd_word in bot_state["commands"]:
                    func_name = bot_state["commands"][cmd_word]
                    call_scope = dict(scope)
                    call_scope["message"] = content
                    call_scope["__discord_message__"] = message
                    execute(functions[func_name]["body"], call_scope)
                    return

            if bot_state["onmessage_func"] and bot_state["onmessage_func"] in functions:
                call_scope = dict(scope)
                call_scope["message"] = content
                call_scope["__discord_message__"] = message
                execute(functions[bot_state["onmessage_func"]]["body"], call_scope)

        client.run(bot_state["token"])

    # ---- helper: print a list nicely (comma separated, no python-style brackets/quotes) instead of raw repr ----
    def format_for_print(value):
        if isinstance(value, list):
            return ", ".join(format_for_print(v) for v in value)
        return value

    # ---- open()/delete()/copy()/close()/send() are handled directly as statements further down in execute() ----

        print(f"[netch error] '{action_text}' isn't a recognized action")

    # ---- main executor: walks a list of statements (from parse_block) and runs them ----
    def execute(statements, scope):
        for stmt in statements:
            if stmt["type"] == "funcdef":
                continue  # already registered globally by collect_functions

            if stmt["type"] == "if":
                if evaluate_condition(stmt["condition"], scope):
                    result = execute(stmt["body"], scope)
                    if result is False:
                        return False
                elif stmt["else_body"]:
                    result = execute(stmt["else_body"], scope)
                    if result is False:
                        return False
                continue

            if stmt["type"] == "while":
                safety_count = 0
                while evaluate_condition(stmt["condition"], scope):
                    if window_mode:
                        if not refresh_window(window_state):
                            break  # window was closed, stop the loop cleanly instead of erroring
                        time.sleep(0.03)  # small pace so the loop doesn't peg the CPU while polling clicks
                    result = execute(stmt["body"], scope)
                    if result is False:
                        return False
                    if not window_mode:
                        safety_count += 1
                        if safety_count > 100000:
                            print("[netch warning] a 'while' loop ran 100,000 times without stopping — "
                                  "stopped it automatically so your program doesn't freeze forever. "
                                  "Double check the condition ever becomes false.")
                            break
                continue

            if stmt["type"] == "repeat":
                try:
                    count = int(resolve_value(stmt["count_expr"], scope))
                except (ValueError, TypeError):
                    print(f"[netch error] 'repeat {stmt['count_expr']} times' — that's not a number")
                    continue
                for _ in range(count):
                    if window_mode:
                        if not refresh_window(window_state):
                            break
                    result = execute(stmt["body"], scope)
                    if result is False:
                        return False
                continue

            if stmt["type"] == "foreach":
                list_name = stmt["list_name"]
                if list_name not in scope or not isinstance(scope[list_name], list):
                    print(f"[netch error] '{list_name}' isn't a list, so 'for each' can't loop over it")
                    continue
                for item in list(scope[list_name]):  # loop over a snapshot so edits mid-loop are safe
                    scope[stmt["item_name"]] = item
                    if window_mode:
                        if not refresh_window(window_state):
                            break
                    result = execute(stmt["body"], scope)
                    if result is False:
                        return False
                continue

            if stmt["type"] == "pyblock":
                try:
                    exec(stmt["code"], scope)
                except Exception as e:
                    print(f"[netch error] the Python block hit an error: {e}")
                continue

            if stmt["type"] == "batblock":
                if platform.system() != "Windows":
                    print("[netch error] <bat> blocks only run on Windows.")
                    continue
                code = stmt["code"]
                try:
                    fillable = {k: v for k, v in scope.items() if isinstance(v, (str, int, float))}
                    code_filled = code.format(**fillable)
                except Exception:
                    code_filled = code  # if {curly} substitution fails for any reason, just run the raw text
                tmp_path = os.path.join(tempfile.gettempdir(), f"netch_bat_{int(time.time() * 1000)}.bat")
                try:
                    with open(tmp_path, "w") as f:
                        f.write(code_filled)
                    result = subprocess.run(["cmd", "/c", tmp_path], capture_output=True, text=True)
                    if result.stdout.strip():
                        print(result.stdout.rstrip())
                    if result.stderr.strip():
                        print(f"[netch error] bat block error output: {result.stderr.rstrip()}")
                except Exception as e:
                    print(f"[netch error] the bat block hit an error: {e}")
                finally:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                continue

            stripped = stmt["text"]

            if not stripped or stripped == '<using.netch>' or stripped == '<window.using>' or stripped.startswith('#'):
                continue

            write_match = re.match(r'^write\((.+?),\s*(.+)\)$', stripped)
            if write_match:
                source_expr, path_expr = write_match.groups()
                source_expr, path_expr = source_expr.strip(), path_expr.strip()
                if source_expr in window_state["textboxes"]:
                    text_to_write = window_state["textboxes"][source_expr].get("1.0", "end-1c")
                else:
                    text_to_write = resolve_value(source_expr, scope)
                path = resolve_value(path_expr, scope)
                path = path[1:-1] if isinstance(path, str) and path.startswith('"') and path.endswith('"') else path
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write("" if text_to_write is None else str(text_to_write))
                    print(f"[netch] saved to {path}")
                except Exception as e:
                    print(f"[netch error] couldn't write to '{path}': {e}")
                continue

            read_match = re.match(r'^read\((.+?),\s*(\w+)\)$', stripped)
            if read_match:
                path_expr, target = read_match.groups()
                path = resolve_value(path_expr.strip(), scope)
                path = path[1:-1] if isinstance(path, str) and path.startswith('"') and path.endswith('"') else path
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        file_text = f.read()
                    if target in window_state["textboxes"]:
                        box = window_state["textboxes"][target]
                        box.delete("1.0", "end")
                        box.insert("1.0", file_text)
                    else:
                        scope[target] = file_text
                    print(f"[netch] loaded {path}")
                except Exception as e:
                    print(f"[netch error] couldn't read '{path}': {e}")
                continue

            open_match = re.match(r'^open\((.+)\)$', stripped)
            if open_match:
                path = resolve_value(open_match.group(1).strip(), scope)
                path = path[1:-1] if isinstance(path, str) and path.startswith('"') and path.endswith('"') else str(path)
                try:
                    if path.startswith('http://') or path.startswith('https://'):
                        import webbrowser
                        webbrowser.open(path)
                        print(f"[netch] opened link {path}")
                    elif platform.system() == "Windows":
                        os.startfile(path)
                        print(f"[netch] opened {path}")
                    elif platform.system() == "Darwin":
                        subprocess.run(["open", path])
                        print(f"[netch] opened {path}")
                    else:
                        subprocess.run(["xdg-open", path])
                        print(f"[netch] opened {path}")
                except Exception as e:
                    print(f"[netch error] couldn't open '{path}': {e}")
                continue

            delete_match = re.match(r'^delete\((.+)\)$', stripped)
            if delete_match:
                path = resolve_value(delete_match.group(1).strip(), scope)
                path = path[1:-1] if isinstance(path, str) and path.startswith('"') and path.endswith('"') else str(path)
                try:
                    os.remove(path)
                    print(f"[netch] deleted {path}")
                except Exception as e:
                    print(f"[netch error] couldn't delete '{path}': {e}")
                continue

            copy_match = re.match(r'^copy\((.+?),\s*(.+)\)$', stripped)
            if copy_match:
                src_expr, dest_expr = copy_match.groups()
                src = resolve_value(src_expr.strip(), scope)
                dest = resolve_value(dest_expr.strip(), scope)
                src = src[1:-1] if isinstance(src, str) and src.startswith('"') and src.endswith('"') else str(src)
                dest = dest[1:-1] if isinstance(dest, str) and dest.startswith('"') and dest.endswith('"') else str(dest)
                try:
                    if platform.system() == "Windows":
                        subprocess.run(["robocopy", os.path.dirname(src) or ".", os.path.dirname(dest) or ".", os.path.basename(src)])
                    else:
                        shutil.copy(src, dest)
                    print(f"[netch] copied {src} -> {dest}")
                except Exception as e:
                    print(f"[netch error] couldn't copy '{src}' to '{dest}': {e}")
                continue

            if stripped == 'close()':
                if window_mode and window_state["root"]:
                    window_state["closed"] = True
                    window_state["root"].destroy()
                print("[netch] closing.")
                continue

            send_match = re.match(r'^send\((.+)\)$', stripped)
            if send_match:
                content = send_match.group(1).strip()
                if content.startswith('"') and content.endswith('"'):
                    text = content[1:-1]
                else:
                    text = resolve_value(content, scope)
                if "__discord_message__" in scope:
                    try:
                        import asyncio
                        msg_obj = scope["__discord_message__"]
                        asyncio.get_event_loop().create_task(msg_obj.channel.send(str(text)))
                        print(f"[netch] bot sent: {text}")
                    except Exception as e:
                        print(f"[netch error] couldn't send discord message: {e}")
                else:
                    print("[netch error] send() only works inside a bot message or command handler")
                continue

            wait_match = re.match(r'^wait\((.+)\)$', stripped)
            if wait_match:
                try:
                    seconds = float(wait_match.group(1).strip())
                    if window_mode and window_state["root"]:
                        end_time = time.time() + seconds
                        while time.time() < end_time:
                            window_state["root"].update()
                            time.sleep(0.03)
                    else:
                        time.sleep(seconds)
                except Exception as e:
                    print(f"[netch error] wait() failed: {e}")
                continue

            wsize_match = re.match(r'^window\.size\((\d+)\s*,\s*(\d+)\)$', stripped)
            if wsize_match:
                if not window_mode:
                    print("[netch error] window.size() only works in a window app (add <window.using> under <using.netch>)")
                    continue
                w, h = wsize_match.groups()
                get_window(window_state).geometry(f"{w}x{h}")
                continue

            wtitle_match = re.match(r'^window\.title\((.+)\)$', stripped)
            if wtitle_match:
                if not window_mode:
                    print("[netch error] window.title() only works in a window app (add <window.using> under <using.netch>)")
                    continue
                title = wtitle_match.group(1).strip().strip('"')
                get_window(window_state).title(title)
                continue

            wtext_match = re.match(r'^window\.text\((.+)\)$', stripped)
            if wtext_match:
                if not window_mode:
                    print("[netch error] window.text() only works in a window app (add <window.using> under <using.netch>)")
                    continue
                content = wtext_match.group(1).strip()
                if content.startswith('"') and content.endswith('"'):
                    text = content[1:-1]
                elif content in scope:
                    text = str(scope[content])
                else:
                    text = content
                root = get_window(window_state)
                label = tk.Label(root, text=text, font=("Segoe UI", 20), bg="#f4f5f7", fg="#2d2d2d")
                label.pack(pady=12, padx=20)
                root.update()
                continue

            wtextbox_match = re.match(r'^window\.textbox\.(\w+)$', stripped)
            if wtextbox_match:
                if not window_mode:
                    print("[netch error] window.textbox only works in a window app (add <window.using> under <using.netch>)")
                    continue
                box_name = wtextbox_match.group(1)
                root = get_window(window_state)
                box = tk.Text(root, height=1, width=10, font=("Segoe UI", 13), wrap="word", relief="flat",
                              bg="white", fg="#2d2d2d", insertbackground="#2d2d2d",
                              padx=10, pady=10, undo=True)
                box.pack(fill="both", expand=True, padx=12, pady=8)
                window_state["textboxes"][box_name] = box
                root.update()
                continue

            wselect_match = re.match(r'^window\.selection\.(\w+)\s*=\s*(dropdown|radio)\((.+)\)$', stripped)
            if wselect_match:
                if not window_mode:
                    print("[netch error] window.selection only works in a window app (add <window.using> under <using.netch>)")
                    continue
                sel_name, sel_type, opts_str = wselect_match.groups()
                options = [o.strip().strip('"') for o in opts_str.split(',') if o.strip()]
                if not options:
                    print(f"[netch error] selection '{sel_name}' needs at least one option")
                    continue
                root = get_window(window_state)
                var = tk.StringVar(root)
                var.set(options[0])
                if sel_type == 'dropdown':
                    widget = tk.OptionMenu(root, var, *options)
                    widget.config(font=("Segoe UI", 12), bg="white", fg="#2d2d2d",
                                  relief="flat", borderwidth=1, padx=10, pady=6, cursor="hand2")
                    widget.pack(pady=8)
                else:
                    frame = tk.Frame(root, bg="#f4f5f7")
                    for opt in options:
                        rb = tk.Radiobutton(frame, text=opt, variable=var, value=opt,
                                             font=("Segoe UI", 12), bg="#f4f5f7", fg="#2d2d2d",
                                             activebackground="#f4f5f7", selectcolor="white", cursor="hand2")
                        rb.pack(anchor="w", pady=2)
                    frame.pack(pady=8)
                    widget = frame
                window_state["selections"][sel_name] = {"var": var, "type": sel_type, "widget": widget}
                root.update()
                continue

            selcolor_match = re.match(r'^selection\.(\w+)\.color\s*=\s*(.+)$', stripped)
            if selcolor_match:
                sel_name, color = selcolor_match.groups()
                color = color.strip().strip('"')
                if sel_name not in window_state["selections"]:
                    print(f"[netch error] selection '{sel_name}' doesn't exist yet")
                    continue
                window_state["selections"][sel_name]["widget"].config(bg=color)
                window_state["root"].update()
                continue

            selsize_match = re.match(r'^selection\.(\w+)\.size\((\d+)\s*,\s*(\d+)\)$', stripped)
            if selsize_match:
                sel_name, w, h = selsize_match.groups()
                if sel_name not in window_state["selections"]:
                    print(f"[netch error] selection '{sel_name}' doesn't exist yet")
                    continue
                window_state["selections"][sel_name]["widget"].config(width=int(int(w) / 10))
                window_state["root"].update()
                continue

            wbutton_bare_match = re.match(r'^window\.button\.(\w+)$', stripped)
            if wbutton_bare_match:
                if not window_mode:
                    print("[netch error] window.button only works in a window app (add <window.using> under <using.netch>)")
                    continue
                btn_name = wbutton_bare_match.group(1)
                window_state["button_clicked"][btn_name] = False
                window_state["button_scope"][btn_name] = scope
                stagger = 20 + 50 * len(window_state["button_geo"])
                window_state["button_geo"][btn_name] = {
                    "x": 20, "y": stagger, "w": 110, "h": 40,
                    "color": "#5865F2", "radius": 0, "font_size": 13,
                    "action_text": None, "label": btn_name,
                }
                render_button(btn_name)
                window_state["root"].update()
                continue

            wbutton_match = re.match(r'^window\.button\.(\w+)\s*=\s*(.+)$', stripped)
            if wbutton_match:
                if not window_mode:
                    print("[netch error] window.button only works in a window app (add <window.using> under <using.netch>)")
                    continue
                btn_name, rhs = wbutton_match.groups()
                rhs = rhs.strip()
                if not rhs.startswith('action '):
                    print(f"[netch warning] button '{btn_name}' is missing the word 'action' before its command. Continue without this button working? (y = continue / n = stop)")
                    choice = input("> ").strip().lower()
                    if choice == 'y':
                        print(f"[netch] continuing, button '{btn_name}' will do nothing when clicked")
                        action_text = None
                    else:
                        print("[netch] stopped.")
                        return False
                else:
                    action_text = rhs[len('action '):].strip()

                window_state["button_clicked"][btn_name] = False
                window_state["button_scope"][btn_name] = scope
                if btn_name in window_state["button_geo"]:
                    window_state["button_geo"][btn_name]["action_text"] = action_text
                else:
                    stagger = 20 + 50 * len(window_state["button_geo"])
                    window_state["button_geo"][btn_name] = {
                        "x": 20, "y": stagger, "w": 110, "h": 40,
                        "color": "#5865F2", "radius": 0, "font_size": 13,
                        "action_text": action_text, "label": btn_name,
                    }
                render_button(btn_name)
                window_state["root"].update()
                continue

            bcolor_match = re.match(r'^button\.(\w+)\.color\s*=\s*(.+)$', stripped)
            if bcolor_match:
                btn_name, color = bcolor_match.groups()
                color = color.strip().strip('"')
                if btn_name not in window_state["button_geo"]:
                    print(f"[netch error] button '{btn_name}' doesn't exist yet")
                    continue
                window_state["button_geo"][btn_name]["color"] = color
                render_button(btn_name)
                window_state["root"].update()
                continue

            bsize_match = re.match(r'^button\.(\w+)\.size\((\d+)\s*,\s*(\d+)\)$', stripped)
            if bsize_match:
                btn_name, w, h = bsize_match.groups()
                if btn_name not in window_state["button_geo"]:
                    print(f"[netch error] button '{btn_name}' doesn't exist yet")
                    continue
                window_state["button_geo"][btn_name]["w"] = int(w)
                window_state["button_geo"][btn_name]["h"] = int(h)
                render_button(btn_name)
                window_state["root"].update()
                continue

            bposition_match = re.match(r'^button\.(\w+)\.position\((\d+)\s*,\s*(\d+)\)$', stripped)
            if bposition_match:
                btn_name, x, y = bposition_match.groups()
                if btn_name not in window_state["button_geo"]:
                    print(f"[netch error] button '{btn_name}' doesn't exist yet")
                    continue
                window_state["button_geo"][btn_name]["x"] = int(x)
                window_state["button_geo"][btn_name]["y"] = int(y)
                render_button(btn_name)
                window_state["root"].update()
                continue

            bround_match = re.match(r'^button\.(\w+)\.round\((\d+)\)$', stripped)
            if bround_match:
                btn_name, radius = bround_match.groups()
                if btn_name not in window_state["button_geo"]:
                    print(f"[netch error] button '{btn_name}' doesn't exist yet")
                    continue
                window_state["button_geo"][btn_name]["radius"] = int(radius)
                render_button(btn_name)
                window_state["root"].update()
                continue

            bfontsize_match = re.match(r'^button\.(\w+)\.fontsize\((\d+)\)$', stripped)
            if bfontsize_match:
                btn_name, fs = bfontsize_match.groups()
                if btn_name not in window_state["button_geo"]:
                    print(f"[netch error] button '{btn_name}' doesn't exist yet")
                    continue
                window_state["button_geo"][btn_name]["font_size"] = int(fs)
                render_button(btn_name)
                window_state["root"].update()
                continue

            tbsize_match = re.match(r'^textbox\.(\w+)\.size\((\d+)\s*,\s*(\d+)\)$', stripped)
            if tbsize_match:
                box_name, w, h = tbsize_match.groups()
                if box_name not in window_state["textboxes"]:
                    print(f"[netch error] textbox '{box_name}' doesn't exist yet")
                    continue
                geo = window_state["box_geometry"].setdefault(box_name, {"x": 0, "y": 0, "w": 200, "h": 100})
                geo["w"], geo["h"] = int(w), int(h)
                widget = window_state["textboxes"][box_name]
                widget.pack_forget()
                widget.place(x=geo["x"], y=geo["y"], width=geo["w"], height=geo["h"])
                window_state["root"].update()
                continue

            tbposition_match = re.match(r'^textbox\.(\w+)\.position\((\d+)\s*,\s*(\d+)\)$', stripped)
            if tbposition_match:
                box_name, x, y = tbposition_match.groups()
                if box_name not in window_state["textboxes"]:
                    print(f"[netch error] textbox '{box_name}' doesn't exist yet")
                    continue
                geo = window_state["box_geometry"].setdefault(box_name, {"x": 0, "y": 0, "w": 200, "h": 100})
                geo["x"], geo["y"] = int(x), int(y)
                widget = window_state["textboxes"][box_name]
                widget.pack_forget()
                widget.place(x=geo["x"], y=geo["y"], width=geo["w"], height=geo["h"])
                window_state["root"].update()
                continue

            tbfontsize_match = re.match(r'^textbox\.(\w+)\.fontsize\((\d+)\)$', stripped)
            if tbfontsize_match:
                box_name, fs = tbfontsize_match.groups()
                if box_name not in window_state["textboxes"]:
                    print(f"[netch error] textbox '{box_name}' doesn't exist yet")
                    continue
                window_state["textboxes"][box_name].config(font=("Segoe UI", int(fs)))
                window_state["root"].update()
                continue

            wtextlabel_match = re.match(r'^window\.text\.(\w+)\s*=\s*(.+)$', stripped)
            if wtextlabel_match:
                if not window_mode:
                    print("[netch error] window.text only works in a window app (add <window.using> under <using.netch>)")
                    continue
                label_name, content = wtextlabel_match.groups()
                content = content.strip()
                if content.startswith('"') and content.endswith('"'):
                    text = content[1:-1]
                elif content in scope:
                    text = str(scope[content])
                else:
                    text = content
                if label_name in window_state["labels"]:
                    window_state["labels"][label_name].config(text=text)
                else:
                    root = get_window(window_state)
                    label = tk.Label(root, text=text, font=("Segoe UI", 16), bg="#f4f5f7", fg="#2d2d2d")
                    label.pack(pady=8, padx=16)
                    window_state["labels"][label_name] = label
                window_state["root"].update()
                continue

            tlposition_match = re.match(r'^text\.(\w+)\.position\((\d+)\s*,\s*(\d+)\)$', stripped)
            if tlposition_match:
                label_name, x, y = tlposition_match.groups()
                if label_name not in window_state["labels"]:
                    print(f"[netch error] text '{label_name}' doesn't exist yet")
                    continue
                widget = window_state["labels"][label_name]
                widget.pack_forget()
                widget.place(x=int(x), y=int(y))
                window_state["root"].update()
                continue

            tlfontsize_match = re.match(r'^text\.(\w+)\.fontsize\((\d+)\)$', stripped)
            if tlfontsize_match:
                label_name, fs = tlfontsize_match.groups()
                if label_name not in window_state["labels"]:
                    print(f"[netch error] text '{label_name}' doesn't exist yet")
                    continue
                window_state["labels"][label_name].config(font=("Segoe UI", int(fs)))
                window_state["root"].update()
                continue

            webpage_match = re.match(r'^display\.webpage\((.+)\)$', stripped)
            if webpage_match:
                url = webpage_match.group(1).strip().strip('"')
                if not re.match(r'^[a-zA-Z][a-zA-Z0-9+.\-]*://', url):
                    url = "https://" + url
                webview = ensure_installed("webview", "pywebview")
                if webview is None:
                    continue
                try:
                    webview.create_window("netch app", url)
                    webview.start()
                except Exception as e:
                    print(f"[netch error] couldn't display webpage: {e}")
                continue

            bottoken_match = re.match(r'^bot\.token\s*=\s*(.+)$', stripped)
            if bottoken_match:
                bot_state["token"] = bottoken_match.group(1).strip().strip('"')
                print("[netch] bot token set")
                continue

            botprefix_match = re.match(r'^bot\.prefix\s*=\s*(.+)$', stripped)
            if botprefix_match:
                bot_state["prefix"] = botprefix_match.group(1).strip().strip('"')
                print(f"[netch] bot command prefix set to '{bot_state['prefix']}'")
                continue

            botcmd_match = re.match(r'^bot\.command\.(\w+)\s*=\s*action\s+(\w+)$', stripped)
            if botcmd_match:
                cmd_name, func_name = botcmd_match.groups()
                if func_name not in functions:
                    print(f"[netch error] function '{func_name}' is not defined")
                    continue
                bot_state["commands"][cmd_name] = func_name
                print(f"[netch] bot command '{bot_state['prefix']}{cmd_name}' will run '{func_name}'")
                continue

            botcmd_bad_match = re.match(r'^bot\.command\.(\w+)\s*=\s*(\w+)$', stripped)
            if botcmd_bad_match:
                cmd_name, maybe_func = botcmd_bad_match.groups()
                print(f"[netch warning] command '{cmd_name}' is missing the word 'action' before its function. Continue without this command working? (y = continue / n = stop)")
                choice = input("> ").strip().lower()
                if choice == 'y':
                    print(f"[netch] continuing, command '{cmd_name}' will do nothing")
                else:
                    print("[netch] stopped.")
                    return False
                continue

            botmsg_match = re.match(r'^bot\.onmessage\s*=\s*action\s+(\w+)$', stripped)
            if botmsg_match:
                func_name = botmsg_match.group(1)
                if func_name not in functions:
                    print(f"[netch error] function '{func_name}' is not defined")
                    continue
                bot_state["onmessage_func"] = func_name
                print(f"[netch] bot will run '{func_name}' on every message")
                continue

            run_match = re.match(r'^run\(\s*(\w+)\s*(?:\((.*?)\))?\s*\)$', stripped)
            if run_match:
                fname, arg_str = run_match.groups()
                if fname == 'bot':
                    start_bot(scope)
                    continue
                if fname not in functions:
                    print(f"[netch error] function '{fname}' is not defined")
                    continue
                args = [a.strip() for a in arg_str.split(',')] if arg_str else []
                fdef = functions[fname]
                call_scope = dict(scope)
                for pname, arg in zip(fdef["params"], args):
                    call_scope[pname] = scope.get(arg, arg.strip('"'))
                result = execute(fdef["body"], call_scope)
                if result is False:
                    return False
                continue

            print_match = re.match(r'^print\((.*)\)$', stripped)
            if print_match:
                content = print_match.group(1).strip()
                sel_read_match = re.match(r'^selection\.(\w+)$', content)
                length_match = re.match(r'^(\w+)\.length$', content)
                math_result = try_math(content, scope)
                if content.startswith('"') and content.endswith('"'):
                    print(content[1:-1])
                elif content.startswith('[') and content.endswith(']') and parse_list_literal(content, scope) is not None:
                    print(format_for_print(parse_list_literal(content, scope)))
                elif sel_read_match and sel_read_match.group(1) in window_state["selections"]:
                    print(window_state["selections"][sel_read_match.group(1)]["var"].get())
                elif length_match and length_match.group(1) in scope and isinstance(scope[length_match.group(1)], (list, str)):
                    print(len(scope[length_match.group(1)]))
                elif content in window_state["textboxes"]:
                    print(window_state["textboxes"][content].get("1.0", "end-1c"))
                elif re.match(r'^\w+\[.+\]$', content):
                    print(format_for_print(resolve_value(content, scope)))
                elif content in scope:
                    print(format_for_print(scope[content]))
                elif math_result is not None:
                    print(math_result)
                else:
                    print(f"[netch error] '{content}' is not defined")
                continue

            listadd_match = re.match(r'^(\w+)\.add\((.+)\)$', stripped)
            if listadd_match:
                list_name, item_expr = listadd_match.groups()
                if list_name not in scope or not isinstance(scope[list_name], list):
                    print(f"[netch error] '{list_name}' isn't a list, so you can't .add() to it")
                    continue
                scope[list_name].append(resolve_value(item_expr, scope))
                continue

            listremove_match = re.match(r'^(\w+)\.remove\((.+)\)$', stripped)
            if listremove_match:
                list_name, item_expr = listremove_match.groups()
                if list_name not in scope or not isinstance(scope[list_name], list):
                    print(f"[netch error] '{list_name}' isn't a list, so you can't .remove() from it")
                    continue
                value_to_remove = resolve_value(item_expr, scope)
                if value_to_remove in scope[list_name]:
                    scope[list_name].remove(value_to_remove)
                else:
                    print(f"[netch warning] '{value_to_remove}' wasn't in '{list_name}', nothing removed")
                continue

            assign_match = re.match(r'^(\w+)\s*=\s*(.+)$', stripped)
            if assign_match:
                var_name, raw_value = assign_match.groups()
                raw_value = raw_value.strip()
                if var_name in window_state["textboxes"]:
                    new_text = resolve_value(raw_value, scope)
                    if isinstance(new_text, str) and new_text.startswith('"') and new_text.endswith('"'):
                        new_text = new_text[1:-1]
                    box = window_state["textboxes"][var_name]
                    box.delete("1.0", "end")
                    box.insert("1.0", str(new_text) if new_text is not None else "")
                    continue
                math_result = try_math(raw_value, scope)
                sel_read_match = re.match(r'^selection\.(\w+)$', raw_value)
                list_literal = parse_list_literal(raw_value, scope) if raw_value.startswith('[') and raw_value.endswith(']') else None
                index_match = re.match(r'^(\w+)\[(.+)\]$', raw_value)
                if raw_value.startswith('"') and raw_value.endswith('"'):
                    scope[var_name] = raw_value[1:-1]
                elif list_literal is not None:
                    scope[var_name] = list_literal
                elif index_match and index_match.group(1) in scope and isinstance(scope[index_match.group(1)], list):
                    scope[var_name] = resolve_value(raw_value, scope)
                elif sel_read_match and sel_read_match.group(1) in window_state["selections"]:
                    scope[var_name] = window_state["selections"][sel_read_match.group(1)]["var"].get()
                elif raw_value in window_state["textboxes"]:
                    scope[var_name] = window_state["textboxes"][raw_value].get("1.0", "end-1c")
                elif raw_value in scope:
                    scope[var_name] = scope[raw_value]
                elif raw_value.lstrip('-').isdigit():
                    scope[var_name] = int(raw_value)
                elif math_result is not None:
                    scope[var_name] = math_result
                else:
                    print(f"[netch warning] '{raw_value}' isn't a variable. Treat it as plain text? (y = continue / n = stop)")
                    choice = input("> ").strip().lower()
                    if choice == 'y':
                        scope[var_name] = raw_value
                        print(f"[netch] continuing, {var_name} = \"{raw_value}\"")
                    else:
                        print("[netch] stopped.")
                        return False
                continue

    execute(program, variables)

    if window_mode and window_state["root"] and not window_state.get("closed"):
        try:
            window_state["root"].mainloop()
        except Exception as e:
            print(f"[netch error] window couldn't stay open: {e}")


if __name__ == "__main__":
    run_netch(sys.argv[1])
