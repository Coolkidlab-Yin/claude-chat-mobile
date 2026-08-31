@echo off
rem Start claude-chat server manually (hidden window).
rem Uses this .cmd file's own folder, so it works no matter where you cloned the repo.
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0server.py"
echo claude-chat starting... check http://127.0.0.1:8899
