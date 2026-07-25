# Starts an ngrok tunnel forwarding port 8080 (the port keep_alive.py's
# Flask server -- and the /github-webhook route -- listens on). Run this
# in its own terminal before starting the bot.

if (-not (Get-Command ngrok -ErrorAction SilentlyContinue)) {
    Write-Host "ngrok isn't installed or not on PATH. Get it from https://ngrok.com/download" -ForegroundColor Red
    exit 1
}

ngrok http 8080