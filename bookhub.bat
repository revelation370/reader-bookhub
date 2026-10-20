@echo off
title bookhub 开关
cd /d "%~dp0"

rem 双击一次=启动服务，再双击=停止服务
rem 网关使用 80 端口（Reader 的网络库不支持带端口的 URL）

netstat -ano | findstr ":80 " | findstr "LISTENING" | findstr ":80" >nul 2>&1
if %errorlevel%==0 goto stop

:start
echo [bookhub] 正在启动...
start "bookhub" /min python server.py
set tries=0

:waitloop
ping -n 3 127.0.0.1 >nul
netstat -ano | findstr "127.0.0.1:80 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 goto up
set /a tries+=1
if %tries% lss 8 goto waitloop
echo [bookhub] 启动失败，请在命令行运行 python server.py 查看报错
goto end

:up
echo [bookhub] 已启动，可以在 Reader 搜索框搜书了
goto end

:stop
echo [bookhub] 正在停止...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":80 " ^| findstr "LISTENING"') do taskkill /PID %%a /F >nul 2>&1
echo [bookhub] 已停止。在线搜书暂不可用（已缓存章节和 txt 不受影响）
goto end

:end
ping -n 4 127.0.0.1 >nul
