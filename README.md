# netch.dot
Readme · MDnetch

The easiest way to start coding — without the ceiling.

netch is a programming language built on a simple idea: your first language shouldn't be a toy, and it shouldn't be a wall either. Most beginner-friendly languages are dead ends — once you outgrow them, you have to start over somewhere else. netch is designed to grow with you, from your very first print() all the way to real, working applications.

We built netch to be readable by anyone, forgiving of mistakes, and powerful enough to actually build things with — desktop apps, Discord bots, AI-powered tools, and more, all from the same simple syntax.


Why netch?


No steep learning curve. If you can read English, you can read netch. function, run(), print() — it reads like plain instructions, not cryptic symbols.
Forgiving, not fragile. Made a typo or an indentation mistake? netch warns you and asks how you'd like to proceed instead of crashing outright.
Room to grow. Start with a simple script today, build a desktop app or a Discord bot tomorrow. netch doesn't box you in.
Batteries included. Windows, buttons, dropdowns, file operations, bots, and AI integration are all built into the language itself — no fighting with external frameworks to get something real running.


Getting Started

Every netch file starts with a required header, similar to how HTML starts with <!DOCTYPE html>:

netch<using.netch>

print("hello world")

That's it — that's a complete, valid netch program.

Variables

netchname = hello
print(name)

Functions

netchfunction greet():
    print("hey there!")

run(greet)

Conditionals & Loops

netchscore = 10

if score > 5:
    print("nice job")
else:
    print("keep going")

repeat 3 times:
    print("netch is fun")

Building Apps

netch isn't limited to the console. Add a second header line to unlock a full desktop app mode:

netch<using.netch>
<window.using>

window.title("My First App")
window.size(400, 300)
window.text("Welcome to netch!")

window.button.click_me = action greet

From here, you can add dropdowns, radio buttons, text inputs, and more — all with the same plain, readable syntax.

Installation


Head to the Releases section (or just grab interpreter.py from this repo) and run the installer:


bashpython installer.py


The installer sets everything up automatically — no extra configuration needed.
Run any netch file directly:


bashnetch yourfile.netch

Roadmap

netch is under active development. Planned additions include:


A full compiler (interpreter today, compiled binaries down the road)
Expanded AI integration
Self-healing code — where small errors can be automatically caught and corrected without stopping your program
A broader standard library for apps and automation


Contributing

netch is early, and there's a lot of ground still to cover. Issues, feedback, and pull requests are welcome — whether that's a bug report, a feature idea, or a fix.
