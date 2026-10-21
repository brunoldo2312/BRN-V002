@echo off
title BRN Node 24/7
cd /d "%~dp0"
color 0A
python supervisor_24h.py
pause