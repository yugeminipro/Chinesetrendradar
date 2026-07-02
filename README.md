# TrendRadar 部署与使用说明

## 环境要求
- 操作系统：Windows 10/11 或 Linux/macOS
- Python：3.9 及以上（推荐 3.10+）
- 网络：可访问模型平台与 Coze 服务（如需）

## 项目结构
- `web_app.py`：网页端服务（Flask），提供主界面与配置界面
- `main.py`：新闻抓取与汇总生成
- `news_filter_image_pipeline.py`：从当日汇总解析新闻、关键词提取与 Bing 图片抓取并生成页面
- `templates/`、`static/`：前端页面与样式
- `config/`：配置文件（`config.yaml` 与模板）
- `output/`：结果输出目录（按日期分组）
- `start.bat`：Windows 一键启动脚本（自动安装依赖并启动网页端）
- `requirements.txt`：依赖列表

## 快速开始（Windows）
1. 双击或在终端运行 `start.bat`
   - 自动检测 Python 与 pip
   - 安装依赖（`requirements.txt`）
   - 初始化数据库与配置
   - 启动服务并提示访问地址 `http://localhost:5000`
2. 浏览器打开 `http://localhost:5000`
3. 进入 `配置设置` 页面，按需完成以下设置：
   - 选择 `AI平台` 与 `模型类型`
   - 填入并保存对应平台的 `API Token`
   - 如需生成文章/视频：填写并保存 Coze 的 `Bot ID` 与 `API Token`
4. 返回 `主界面`，点击 `🚀 开始获取新闻`，完成后可点击 `🤖 AI智能筛选`

提示：启动脚本输出的日志中按需查看依赖安装与应用启动信息。按 `Ctrl + C` 可停止服务。

## 手动启动（跨平台）
```bash
# 进入部署目录
cd TrendRadar_Deploy

# 安装依赖
python -m pip install -r requirements.txt

# 启动网页端
python web_app.py
```
- 默认端口 `5000`，可通过环境变量指定：`PORT=8000 python web_app.py`
- 首次启动会在 `config/` 生成 `config.yaml`（若不存在）。

## 配置说明
- 路径：`TrendRadar_Deploy/config/config.yaml`
- 关键项：
  - `platform`：AI平台（`volcengine`/`siliconflow`/`aliyun`）
  - `tokens`：各平台 `API Key`
  - `coze.workflow` 与 `coze.chat`：`bot_id` 与 `api_token`
  - `crawler.default_proxy`：HTTP 代理，如需（例如 `http://127.0.0.1:7890`）
  - `notification`：消息通道（默认禁用）

前端保存：在网页 `配置设置` 页面编辑后点击保存，即会写入此配置；无需手工修改。

## 输出目录结构
- 根目录：`TrendRadar_Deploy/output/`
- 日期分组：`YYYY年MM月DD日/`
- HTML 文件：
  - `当日汇总.html`（新闻抓取主任务输出）
  - `图片抓取结果_Bing.html`（管道脚本生成：新闻+关键词+图片）

## 常用操作
- 新闻获取：`主界面` 点击 `🚀 开始获取新闻`
- AI筛选：生成当日汇总后，设置 `筛选数量` 并点击 `🤖 AI智能筛选`
- 结果管理：`结果预览列表` 中对文件 `预览`/`下载`；支持 `清空所有结果`
- 循环执行：设置 `时间间隔（分钟）`，点击 `启动循环`（支持停止与状态查看）
- 文章/视频生成：预览 `图片抓取结果_Bing.html`，在右侧预览窗格中选择新闻与图片并生成

## 后端接口（进阶）
- 任务控制：
  - `POST /api/run_main`、`POST /api/stop_main`
  - `POST /api/run_filter`、`POST /api/stop_filter`
  - `GET /api/task_status/main_task`、`GET /api/task_status/filter_task`
- 结果管理：
  - `GET /api/results`、`GET /api/preview/:path`
  - `POST /api/clear_results`
- 配置：
  - `GET/POST /api/config`
  - `GET/POST /api/news_filter_config`
- 定时循环：
  - `POST /api/schedule/start`、`POST /api/schedule/stop`
  - `GET /api/schedule/status`

## 关键词与图片抓取管道
- 直接运行（独立生成图片结果页）：
```bash
python -u news_filter_image_pipeline.py
```
- 行为：
  - 解析最新的 `当日汇总.html`
  - 使用当前平台/模型提取关键词（最多3个/新闻）
  - 调用 Bing 图片搜索，按关键词抓取图片（每关键词 10 张）
  - 构建输出页面 `图片抓取结果_Bing.html`
- 已内置规则：不抓取 `.gif` 图片链接、支持域名白/黑名单过滤（`config.yaml` 可配置）

## 部署到服务器（Linux）
```bash
cd TrendRadar_Deploy
python -m venv venv && source venv/bin/activate
python -m pip install -r requirements.txt
PORT=5000 python web_app.py  # 或用 screen/nohup 保持前台运行
```
- 开放端口：在云服务器安全组与防火墙中开放相应端口（默认 5000）
- 运行守护：可使用 `screen`/`tmux`/`nohup` 维持进程运行；如需生产环境守护，建议配合系统服务或反向代理

## 常见问题
- 依赖安装失败：检查网络与权限；中国大陆网络建议使用代理或镜像源
- AI筛选按钮灰化：先运行新闻获取，确保当天目录生成 `当日汇总.html`
- 文章/视频生成异常：核对 Coze `bot_id` 与 `api_token`，确保网络可访问
- 无结果或预览失败：点击 `刷新结果列表`，或检查 `output/` 目录权限

## 安全建议
- 不要将密钥（API Token）提交到版本库
- 配置文件中的密钥仅本地保存；服务器部署时请限制文件权限

## 版本升级
- 依赖升级：`python -m pip install -r requirements.txt --upgrade`
- 应用启动脚本 `start.bat` 会尝试先升级 pip 再安装依赖

## 反馈与支持
- 问题与建议可在项目主页反馈
- 如需定制部署或功能扩展，请联系维护者