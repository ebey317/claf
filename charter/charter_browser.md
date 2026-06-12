BROWSER = SENSEI ONLY
New tab: mcp__sensei__tab_create, then mcp__sensei__read_full FIRST (full DOM, not just viewport). Screenshot only to confirm.
Click: read_full → pick CSS selector → mcp__sensei__click, one at a time, screenshot after.
Fill stubborn React/SPA fields: mcp__sensei__js_eval or xdotool, never blind fill.
Search: mcp__sensei__search for web search. mcp__sensei__browse to navigate to a URL directly.
Documentation/help requests: if the user asks for docs/help/manual and didn't give an exact URL, use mcp__sensei__search to find the page first. Do not scroll the marketing homepage hunting for a footer docs link.
NEVER use claude-in-chrome tools. NEVER shell out to google-chrome. Sensei is the only browser path.
