@echo off
rem Doc Reader launcher for Windows. See run-doc-reader.ps1 for details.
rem   run-doc-reader.cmd            start everything and open the web app
rem   run-doc-reader.cmd status     show service health
rem   run-doc-reader.cmd stop
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-doc-reader.ps1" %*
exit /b %ERRORLEVEL%
