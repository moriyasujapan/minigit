@echo off
rem ============================================================
rem  join.cmd - join FILE.part.000, .001, ... into FILE (cmd.exe only)
rem  usage : join.cmd [FILE (default minigit.py)]
rem  the result is byte-identical to the parts (copy /b).
rem  if FILE.sha256 exists, the hash is verified with certutil.
rem ============================================================
setlocal EnableDelayedExpansion

set "F=%~1"
if not defined F set "F=minigit.py"
if not exist "%F%.part.000" (
  echo parts not found: %F%.part.000 ...
  exit /b 1
)

set /a CNT=0
for /f "delims=" %%p in ('dir /b /on "%F%.part.???"') do (
  if !CNT! equ 0 (
    copy /b /y "%%p" "%F%" >nul
  ) else (
    copy /b /y "%F%"+"%%p" "%F%" >nul
  )
  set /a CNT+=1
)
for %%f in ("%F%") do echo joined !CNT! parts -^> %%~nxf  %%~zf bytes

if not exist "%F%.sha256" goto :eof

set "EXP="
for /f "usebackq tokens=1" %%a in ("%F%.sha256") do if not defined EXP set "EXP=%%a"
set "ACT="
for /f "skip=1 delims=" %%h in ('certutil -hashfile "%F%" SHA256') do if not defined ACT set "ACT=%%h"
set "ACT=!ACT: =!"
if /i "!ACT!"=="!EXP!" (
  echo SHA256 OK: !ACT!
) else (
  echo SHA256 MISMATCH
  echo   expected: !EXP!
  echo   actual  : !ACT!
  echo   ^(line endings may have been converted^)
  exit /b 2
)
endlocal
exit /b 0
