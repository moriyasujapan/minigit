@echo off
rem ============================================================
rem  split.cmd - split a text file by lines (cmd.exe only)
rem  usage : split.cmd FILE [LINES_PER_PART (default 400)]
rem  output: FILE.part.000, FILE.part.001, ...
rem  note  : each line is rewritten with CRLF line endings.
rem          Python source works fine with CRLF, but the SHA256
rem          will differ from the original LF file.
rem ============================================================
setlocal DisableDelayedExpansion

if "%~1"=="" (
  echo usage: split.cmd FILE [LINES_PER_PART]
  exit /b 1
)
set "F=%~1"
set "N=%~2"
if not defined N set "N=400"
if not exist "%F%" (
  echo file not found: %F%
  exit /b 1
)

rem keep the current code page and switch to UTF-8 while copying lines
for /f "tokens=2 delims=:." %%c in ('chcp') do set "OLDCP=%%c"
chcp 65001 >nul

del /q "%F%.part.???" 2>nul
set /a CNT=0

rem findstr /n "^" keeps empty lines ("123:" prefix is removed below)
for /f "delims=" %%L in ('findstr /n "^" "%F%"') do (
  set "LINE=%%L"
  set /a CNT+=1
  setlocal EnableDelayedExpansion
  set /a "P=(CNT-1)/N"
  set "IDX=00!P!"
  >>"!F!.part.!IDX:~-3!" echo(!LINE:*:=!
  endlocal
)

chcp %OLDCP% >nul
echo %CNT% lines
for %%p in ("%F%.part.???") do echo %%~nxp  %%~zp bytes
endlocal
exit /b 0
