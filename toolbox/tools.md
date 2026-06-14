# CLAF Toolbox — minted deterministic tools

Add a tool here, then run `python3 ~/projects/claf/toolbox/mint_tool.py` to regenerate
`registry.json`, `charter_core.md` mappings, and any missing Python stubs.

## thunderbird_summary
- **command:** summarize my email accounts | Thunderbird summary | use Thunderbird for email
- **args:** (none)
- **description:** Summarize all cached Thunderbird email accounts using the local scanner.
- **module:** thunderbird_summary.py

## open_website
- **command:** go to [url] | open [url] | visit [website] | let's go to [url] | open tab
- **args:** url (string, optional)
- **description:** Open a website in Chrome via Sensei MCP. Defaults to kimi.com if no URL is given.
- **module:** open_website.py
- **never:** xdg-open, xdg-browser, webbrowser, subprocess
- **example:** go to youtube.com → Bash: python3 ~/projects/claf/toolbox/run_tool.py open_website '{"url": "youtube.com"}'
- **example:** open tab → Bash: python3 ~/projects/claf/toolbox/run_tool.py open_website '{}'

## daddy_dom
- **command:** read the page | scan this website | daddy dom | capture page | read dom
- **args:** url (string, optional)
- **description:** Read the full DOM of a website. If no URL is given, defaults to kimi.com. Returns structured elements.
- **module:** daddy_dom.py
- **never:** curl, wget, requests, BeautifulSoup, selenium
- **example:** read the page → Bash: python3 ~/projects/claf/toolbox/run_tool.py daddy_dom '{}'
- **example:** scan kimi.com → Bash: python3 ~/projects/claf/toolbox/run_tool.py daddy_dom '{"url": "kimi.com"}'

## system_check
- **command:** check system | system status | list usb | show disks | check mounts | what processes | system_check
- **args:** target (string, required) — one of: usb, disks, mounts, processes, services
- **description:** Inspect local machine state (USB devices, disks, mounts, processes, failed services). No network access.
- **module:** system_check.py
- **never:** curl, wget, requests, browser, internet
- **example:** check system usb → Bash: python3 ~/projects/claf/toolbox/run_tool.py system_check '{"target": "usb"}'

## open_app
- **command:** open [app] | launch [app] | start [app] | open hypnotix | open thunderbird | open writer | open terminal | open gnome-terminal | open files | open browser | open chrome | open firefox | open vlc | open calculator | open settings | open ollama
- **args:** app (string, required)
- **description:** Launch a local desktop application by name. Unknown names are looked up in PATH, so typing "open ollama" works like typing `ollama` in a terminal.
- **module:** open_app.py
- **never:** subprocess.run blocking, xdg-open
- **example:** open hypnotix → Bash: python3 ~/projects/claf/toolbox/run_tool.py open_app '{"app": "hypnotix"}'
- **example:** open writer → Bash: python3 ~/projects/claf/toolbox/run_tool.py open_app '{"app": "writer"}'
- **example:** open ollama → Bash: python3 ~/projects/claf/toolbox/run_tool.py open_app '{"app": "ollama"}'
