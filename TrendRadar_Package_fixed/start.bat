@echo off
chcp 65001 >nul
echo ========================================
echo    TrendRadar News Filter System
echo ========================================
echo.
echo [INFO] Current directory: %CD%
echo [INFO] Checking environment...
echo.

:: Check Python installation (multiple methods)
echo [DEBUG] Checking Python environment...

:: Method 1: Try 'python' command
python --version >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON_CMD=python
    goto :python_found
)

:: Method 2: Try 'py' command (Python Launcher)
py --version >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON_CMD=py
    goto :python_found
)

:: Method 3: Try 'python3' command
python3 --version >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON_CMD=python3
    goto :python_found
)

:: Method 4: Check common installation paths
for %%p in (
    "%LOCALAPPDATA%\Programs\Python\Python*\python.exe"
    "%PROGRAMFILES%\Python*\python.exe"
    "%PROGRAMFILES(X86)%\Python*\python.exe"
    "%USERPROFILE%\AppData\Local\Microsoft\WindowsApps\python.exe"
) do (
    if exist "%%p" (
        set PYTHON_CMD="%%p"
        goto :python_found
    )
)

:: Python not found
echo [ERROR] Python not detected!
echo.
echo Possible solutions:
echo 1. Install Python from https://www.python.org/downloads/
echo 2. Make sure Python is added to PATH during installation
echo 3. If using VSCode, try running this script from VSCode terminal
echo 4. If using Anaconda, activate your environment first
echo.
echo Press any key to exit...
pause >nul
exit /b 1

:python_found

echo [INFO] Python environment check passed
echo.

:: Check pip installation
echo [DEBUG] Checking pip environment...
%PYTHON_CMD% -m pip --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] pip not available!
    echo Please ensure pip is installed with Python
    echo.
    echo Press any key to exit...
    pause >nul
    exit /b 1
)

echo [INFO] pip check passed
echo Creating necessary directory structure...
if not exist "data" mkdir data
if not exist "config" mkdir config
if not exist "output" mkdir output
if not exist "templates" mkdir templates
if not exist "static" mkdir static

echo Initializing configuration files...
if not exist "config\config.yaml" (
    if exist "config\config_template.yaml" (
        copy "config\config_template.yaml" "config\config.yaml" >nul
        echo Configuration file created from template
        echo Please configure your API tokens in the web interface
    ) else (
        echo WARNING: Configuration template not found
    )
)

echo [INFO] Directory structure check completed
echo.

:: Check requirements.txt exists
if not exist "requirements.txt" (
    echo [ERROR] requirements.txt file not found!
    echo Please ensure project files are complete
    echo.
    echo Press any key to exit...
    pause >nul
    exit /b 1
)

:: Install dependencies
echo [INFO] Installing dependencies...
echo [INFO] This may take a few minutes, please wait...
echo.

:: Try to upgrade pip first
echo [INFO] Upgrading pip...
%PYTHON_CMD% -m pip install --upgrade pip --quiet

:: Install dependencies
echo [INFO] Installing required packages...

%PYTHON_CMD% -m pip install -r requirements.txt --no-cache-dir --timeout 300
if %errorlevel% equ 0 (
    echo [SUCCESS] All dependencies installed successfully!
    goto :dependencies_done
)

echo.
echo [ERROR] Dependency installation failed!
echo.
echo Possible solutions:
echo 1. Check your internet connection
echo 2. Try running as administrator
echo 3. Use a VPN if in China
echo 4. Install dependencies manually:
echo    %PYTHON_CMD% -m pip install requests pytz PyYAML httpx openai Flask Flask-CORS
echo.
echo Press any key to exit...
pause >nul
exit /b 1

:dependencies_done

echo.
echo [SUCCESS] Dependencies installed successfully
echo.

:: Check Flask application file exists
if not exist "web_app.py" (
    echo [ERROR] web_app.py file not found!
    echo Please ensure project files are complete
    echo.
    echo Press any key to exit...
    pause >nul
    exit /b 1
)

:: Initialize database
echo [INFO] Initializing database...
%PYTHON_CMD% -c "from db import init_db; init_db(); print('Database initialization completed')"
if %errorlevel% neq 0 (
    echo [WARNING] Database initialization may have failed, but program will continue
)

echo.
echo [INFO] Starting web application...
echo After successful startup, please visit: http://localhost:5000
echo Press Ctrl+C to stop the service
echo.
echo ========================================
echo.

:: Start Flask application
%PYTHON_CMD% web_app.py

echo.
echo [INFO] Application has stopped running
echo Press any key to exit...
pause >nul