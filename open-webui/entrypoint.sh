#!/usr/bin/env bash
# Inject custom serif CSS and Dynatrace RUM into Open WebUI's index.html before starting the app.

CUSTOM_CSS="/app/custom.css"
INDEX_HTML="/app/build/index.html"

if [ -f "$CUSTOM_CSS" ] && [ -f "$INDEX_HTML" ]; then
  # Only inject once (idempotent across restarts with the same volume)
  if ! grep -q 'id="custom-serif-theme"' "$INDEX_HTML"; then
    echo "[custom-entrypoint] Injecting serif typography CSS into Open WebUI..."
    python3 -c "
css = open('$CUSTOM_CSS').read()
html = open('$INDEX_HTML').read()
tag = '<style id=\"custom-serif-theme\">\n' + css + '\n</style>\n</head>'
html = html.replace('</head>', tag, 1)
open('$INDEX_HTML', 'w').write(html)
"
    echo "[custom-entrypoint] CSS injected successfully."
  else
    echo "[custom-entrypoint] Serif CSS already present, skipping injection."
  fi
else
  echo "[custom-entrypoint] Warning: custom.css or index.html not found, skipping CSS injection."
fi

# Inject Dynatrace RUM JS tag
if [ -f "$INDEX_HTML" ]; then
  if ! grep -q 'js-cdn.dynatrace.com' "$INDEX_HTML"; then
    echo "[custom-entrypoint] Injecting Dynatrace RUM tag..."
    sed -i 's|</head>|<script type="text/javascript" src="https://js-cdn.dynatrace.com/jstag/18b1df4492a/bf12470wrz/1415e268575ba0e2_complete.js" crossorigin="anonymous"></script>\n</head>|' "$INDEX_HTML"
    echo "[custom-entrypoint] Dynatrace RUM tag injected successfully."
  else
    echo "[custom-entrypoint] Dynatrace RUM tag already present, skipping."
  fi

  # Inject dtrum.identifyUser() script
  if ! grep -q 'dtrum-identify-user' "$INDEX_HTML"; then
    echo "[custom-entrypoint] Injecting Dynatrace user tagging script..."
    sed -i 's|</body>|<script id="dtrum-identify-user">(function(){function tag(){var t=localStorage.getItem("token");if(t){try{var p=JSON.parse(atob(t.split(".")[1]));if(p.name){if(typeof dtrum!=="undefined"){dtrum.identifyUser(p.name)}else if(typeof dtrum==="undefined"){setTimeout(tag,2000)}}}catch(e){}}}if(document.readyState==="complete"){tag()}else{window.addEventListener("load",tag)}var orig=Storage.prototype.setItem;Storage.prototype.setItem=function(k,v){orig.apply(this,arguments);if(k==="token"\|\|k.endsWith("-token")){setTimeout(tag,500)}}})()</script>\n</body>|' "$INDEX_HTML"
    echo "[custom-entrypoint] User tagging script injected successfully."
  else
    echo "[custom-entrypoint] User tagging script already present, skipping."
  fi
fi

# Hand off to the original Open WebUI start script
exec bash /app/backend/start.sh "$@"
