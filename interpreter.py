import sys
import re
import os
import shutil
import subprocess
import platform
import time

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
        elif s["type"] in ("while", "repeat"):
            collect_functions(s["body"], out)


def run_netch(filepath):
    with open(filepath, 'r') as f:
        raw_lines = [line.rstrip('\n') for line in f.readlines()]

    if not raw_lines or raw_lines[0].strip() != '<using.netch>':
        print("[netch warning] missing <using.netch> at top of file, running anyway...\n")

    window_mode = len(raw_lines) > 1 and raw_lines[1].strip() == '<window.using>'
    window_state = {"root": None, "widgets": [], "buttons": {}, "selections": {}}
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

    # ---- helper: resolve a single value (string literal, number, variable, or selection.name) ----
    def resolve_value(token, scope):
        token = token.strip()
        if token.startswith('"') and token.endswith('"'):
            return token[1:-1]
        sel_match = re.match(r'^selection\.(\w+)$', token)
        if sel_match and sel_match.group(1) in window_state["selections"]:
            return window_state["selections"][sel_match.group(1)]["var"].get()
        if token in scope:
            return scope[token]
        try:
            return float(token) if '.' in token else int(token)
        except ValueError:
            return token

    # ---- helper: evaluate an if-condition like `chosen == "Pizza"` ----
    def evaluate_condition(cond, scope):
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
    def get_window(state):
        if state["root"] is None:
            if tk is None:
                raise RuntimeError("tkinter isn't available in this environment")
            state["root"] = tk.Tk()
            state["root"].title("netch app")
            state["root"].configure(bg="#f4f5f7")
            state["root"].geometry("500x400")
            state["root"].minsize(250, 150)
        return state["root"]

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

    # ---- helper: run a single action command (used by open/delete/copy/close/send AND buttons AND bot commands) ----
    def run_action(action_text, scope=None):
        action_text = action_text.strip()

        m = re.match(r'^send\((.+)\)$', action_text)
        if m:
            content = m.group(1).strip()
            if content.startswith('"') and content.endswith('"'):
                text = content[1:-1]
            elif scope and content in scope:
                text = str(scope[content])
            else:
                text = content
            if scope and "__discord_message__" in scope:
                try:
                    import asyncio
                    msg_obj = scope["__discord_message__"]
                    asyncio.get_event_loop().create_task(msg_obj.channel.send(text))
                    print(f"[netch] bot sent: {text}")
                except Exception as e:
                    print(f"[netch error] couldn't send discord message: {e}")
            else:
                print("[netch error] send() only works inside a bot message or command handler")
            return

        m = re.match(r'^open\((.+)\)$', action_text)
        if m:
            path = m.group(1).strip().strip('"')
            try:
                if path.startswith('http://') or path.startswith('https://'):
                    import webbrowser
                    webbrowser.open(path)
                    print(f"[netch] opened link {path}")
                    return
                if platform.system() == "Windows":
                    os.startfile(path)
                elif platform.system() == "Darwin":
                    subprocess.run(["open", path])
                else:
                    subprocess.run(["xdg-open", path])
                print(f"[netch] opened {path}")
            except Exception as e:
                print(f"[netch error] couldn't open '{path}': {e}")
            return

        m = re.match(r'^delete\((.+)\)$', action_text)
        if m:
            path = m.group(1).strip().strip('"')
            try:
                os.remove(path)
                print(f"[netch] deleted {path}")
            except Exception as e:
                print(f"[netch error] couldn't delete '{path}': {e}")
            return

        m = re.match(r'^copy\((.+?),\s*(.+)\)$', action_text)
        if m:
            src, dest = m.groups()
            src, dest = src.strip().strip('"'), dest.strip().strip('"')
            try:
                if platform.system() == "Windows":
                    subprocess.run(["robocopy", os.path.dirname(src) or ".", os.path.dirname(dest) or ".", os.path.basename(src)])
                else:
                    shutil.copy(src, dest)
                print(f"[netch] copied {src} -> {dest}")
            except Exception as e:
                print(f"[netch error] couldn't copy '{src}' to '{dest}': {e}")
            return

        if action_text == 'close()':
            if window_mode and window_state["root"]:
                window_state["root"].destroy()
            print("[netch] closing.")
            return

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
                    if window_mode and window_state["root"]:
                        window_state["root"].update()
                    result = execute(stmt["body"], scope)
                    if result is False:
                        return False
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
                    if window_mode and window_state["root"]:
                        window_state["root"].update()
                    result = execute(stmt["body"], scope)
                    if result is False:
                        return False
                continue

            stripped = stmt["text"]

            if not stripped or stripped == '<using.netch>' or stripped == '<window.using>' or stripped.startswith('#'):
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
                root = get_window(window_state)
                btn = tk.Button(root, text=btn_name, font=("Segoe UI", 13), bg="#4a90d9", fg="white",
                                 activebackground="#3a7bc8", activeforeground="white",
                                 relief="flat", borderwidth=0, padx=16, pady=8, cursor="hand2",
                                 command=(lambda a=action_text, s=scope: execute([{"type": "line", "text": a}], s)) if action_text else (lambda: None))
                btn.pack(pady=8)
                window_state["buttons"][btn_name] = btn
                root.update()
                continue

            bcolor_match = re.match(r'^button\.(\w+)\.color\s*=\s*(.+)$', stripped)
            if bcolor_match:
                btn_name, color = bcolor_match.groups()
                color = color.strip().strip('"')
                if btn_name not in window_state["buttons"]:
                    print(f"[netch error] button '{btn_name}' doesn't exist yet")
                    continue
                window_state["buttons"][btn_name].config(bg=color)
                window_state["root"].update()
                continue

            bsize_match = re.match(r'^button\.(\w+)\.size\((\d+)\s*,\s*(\d+)\)$', stripped)
            if bsize_match:
                btn_name, w, h = bsize_match.groups()
                if btn_name not in window_state["buttons"]:
                    print(f"[netch error] button '{btn_name}' doesn't exist yet")
                    continue
                window_state["buttons"][btn_name].config(width=int(int(w) / 10), height=int(int(h) / 20))
                window_state["root"].update()
                continue

            webpage_match = re.match(r'^display\.webpage\((.+)\)$', stripped)
            if webpage_match:
                url = webpage_match.group(1).strip().strip('"')
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
                math_result = try_math(content, scope)
                if content.startswith('"') and content.endswith('"'):
                    print(content[1:-1])
                elif sel_read_match and sel_read_match.group(1) in window_state["selections"]:
                    print(window_state["selections"][sel_read_match.group(1)]["var"].get())
                elif content in scope:
                    print(scope[content])
                elif math_result is not None:
                    print(math_result)
                else:
                    print(f"[netch error] '{content}' is not defined")
                continue

            assign_match = re.match(r'^(\w+)\s*=\s*(.+)$', stripped)
            if assign_match:
                var_name, raw_value = assign_match.groups()
                raw_value = raw_value.strip()
                math_result = try_math(raw_value, scope)
                sel_read_match = re.match(r'^selection\.(\w+)$', raw_value)
                if raw_value.startswith('"') and raw_value.endswith('"'):
                    scope[var_name] = raw_value[1:-1]
                elif sel_read_match and sel_read_match.group(1) in window_state["selections"]:
                    scope[var_name] = window_state["selections"][sel_read_match.group(1)]["var"].get()
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

    if window_mode and window_state["root"]:
        try:
            window_state["root"].mainloop()
        except Exception as e:
            print(f"[netch error] window couldn't stay open: {e}")


if __name__ == "__main__":
    run_netch(sys.argv[1])
