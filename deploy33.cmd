@echo off
rem deploy33.cmd - one-command release of video-transcribe-platform
rem to the 10.0.0.33 GPU host. Wraps deployment-assets/tools/deploy33.sh.
rem
rem Usage:
rem   deploy33.cmd              full release (api + web + moss)
rem   deploy33.cmd --dry-run    package locally and show the plan
rem   deploy33.cmd --rollback   roll back to the previous image tag
rem
rem Credentials live in deployment-assets\tools\.remote.33.env (gitignored).
setlocal

set "ROOT=%~dp0"
set "SCRIPT=%ROOT%deployment-assets\tools\deploy33.sh"

set "BASH=D:\codingTools\WorkBuddy\resources\vendor\PortableGit\usr\bin\bash.exe"
if not exist "%BASH%" (
  echo [deploy33] Git Bash not found at "%BASH%".
  echo           Edit this file and point BASH at your Git for Windows install.
  exit /b 1
)

if not exist "%SCRIPT%" (
  echo [deploy33] missing %SCRIPT%
  exit /b 1
)

"%BASH%" "%SCRIPT%" %*
