@echo off
chcp 65001 >nul
setlocal

:: 读取 models.json 提取 deepseek 凭据
for /f "tokens=*" %%a in ('python -c "import json, os; p=os.path.expanduser('~/.workbuddy/models.json'); data=json.load(open(p, encoding='utf-8')); d=next(i for i in data if i.get('id')=='deepseek-v4-pro'); print(f'{d[\"url\"]}|{d[\"apiKey\"]}|deepseek-v4-pro')" 2^>nul') do (
    set "CREDS=%%a"
)

if "%CREDS%"=="" (
    echo [PromptBox] 未能在 ~/.workbuddy/models.json 中找到 deepseek 配置
    pause
    exit /b 1
)

for /f "tokens=1,2,3 delims=|" %%i in ("%CREDS%") do (
    set "PROMPTBOX_REPAIR_API_BASE=https://api.deepseek.com/v1"
    set "PROMPTBOX_REPAIR_API_KEY=%%j"
    set "PROMPTBOX_REPAIR_MODEL=deepseek-v4-pro"
)

echo [PromptBox] 已注入 DeepSeek AI 修复服务:
echo   - 接口: %PROMPTBOX_REPAIR_API_BASE%
echo   - 模型: %PROMPTBOX_REPAIR_MODEL%
echo [PromptBox] 正在启动主程序...

start "" "%~dp0dist\PromptBox.exe"
exit /b 0
