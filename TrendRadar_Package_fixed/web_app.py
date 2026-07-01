#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory, Response
from flask_cors import CORS
import yaml
import requests
import re
# 使用 OpenAI 兼容客户端
# from volcenginesdkarkruntime import Ark
from openai import OpenAI as _OpenAIClient
# 本地Ark配置常量，替代对news_filter.py的依赖
ARK_API_ENDPOINT = 'https://ark.cn-beijing.volces.com/api/v3'
ARK_DOUHAO_MODEL = 'doubao-seed-1-6-flash-250828'
from db import init_db, save_app_config, load_app_config, add_run_history


app = Flask(__name__)
CORS(app)

# 确保所有JSON响应按UTF-8输出，避免中文被错误编码
app.config['JSON_AS_ASCII'] = False

# 统一基准目录与关键路径
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / 'output'
CONFIG_PATH = BASE_DIR / 'config' / 'config.yaml'
CONFIG_TEMPLATE_PATH = BASE_DIR / 'config' / 'config_template.yaml'

# 初始化数据库
init_db()

# 全局变量存储任务状态
task_status = {
    'main_task': {'status': 'idle', 'progress': 0, 'message': '', 'result': None, 'logs': []},
    'filter_task': {'status': 'idle', 'progress': 0, 'message': '', 'result': None, 'logs': []}
}

# 运行中子进程与停止标志（支持“停止”按钮）
main_process = None
filter_process = None
main_stop_requested = False
filter_stop_requested = False

def load_config():
    """加载配置文件"""
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except Exception as e:
        return None

def save_config(config):
    """保存配置文件：合并写入，避免覆盖其他配置"""
    try:
        # 读取现有配置
        existing = {}
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                existing = yaml.safe_load(f) or {}
        except Exception:
            existing = {}
        # 合并顶层键，针对 tokens 进行字典合并
        merged = dict(existing)
        for k, v in (config or {}).items():
            if k == 'tokens':
                tv = dict(existing.get('tokens', {}))
                tv.update(v or {})
                merged['tokens'] = tv
            elif isinstance(v, dict) and isinstance(existing.get(k), dict):
                m = dict(existing.get(k, {}))
                m.update(v)
                merged[k] = m
            else:
                merged[k] = v
        # 写回文件
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            yaml.dump(merged, f, default_flow_style=False, allow_unicode=True)
        return True
    except Exception as e:
        return False

def load_all_platforms():
    """加载支持平台全集：严格以模板为准，并用当前配置覆盖名称（不新增未知ID）"""
    try:
        template_map = {}
        # 从模板加载支持的全集
        try:
            with open(CONFIG_TEMPLATE_PATH, 'r', encoding='utf-8') as f:
                tpl = yaml.safe_load(f) or {}
                for p in tpl.get('platforms', []) or []:
                    if isinstance(p, dict) and 'id' in p:
                        template_map[p['id']] = {'id': p['id'], 'name': p.get('name', p['id'])}
        except Exception:
            template_map = {}

        # 仅覆盖名称：如果当前配置中有同ID，则以配置名称为准
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                cfg = yaml.safe_load(f) or {}
                for p in cfg.get('platforms', []) or []:
                    if isinstance(p, dict) and 'id' in p and p['id'] in template_map:
                        template_map[p['id']]['name'] = p.get('name', template_map[p['id']]['name'])
        except Exception:
            pass

        # 返回按名称排序的列表
        return sorted(template_map.values(), key=lambda x: x.get('name', x['id']))
    except Exception:
        return []

def run_main_script():
    """运行main.py脚本"""
    global task_status, main_process, main_stop_requested
    
    try:
        task_status['main_task']['status'] = 'running'
        task_status['main_task']['progress'] = 10
        task_status['main_task']['message'] = '正在启动新闻爬取...'
        task_status['main_task']['logs'] = []
        
        # 动态获取平台总数
        current_config = load_config() or {}
        total_platforms = len(current_config.get('platforms', []))
        task_status['main_task']['message'] = f"发现{total_platforms}个平台，开始爬取..."
        
        # 运行main.py，合并stderr到stdout以便统一日志展示
        main_stop_requested = False
        main_process = subprocess.Popen(
            ['python', '-u', 'main.py'],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='ignore',
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1', 'PYTHONUNBUFFERED': '1', 'WEB_APP': '1'},
            cwd=str(BASE_DIR)
        )
        
        task_status['main_task']['progress'] = 30
        task_status['main_task']['message'] = '正在爬取新闻数据...'
        
        # 流式读取输出，支持进度更新和日志滚动
        platform_count = 0
        # total_platforms = 5  # 以实际平台数为准
        for line in iter(main_process.stdout.readline, ''):
            # 若收到停止请求，主动结束循环并尝试终止子进程
            if main_stop_requested:
                try:
                    main_process.terminate()
                except Exception:
                    pass
                break
            if not line:
                break
            line = line.strip()
            if line:
                # 仅通过 PROGRESS: 日志更新进度
                if line.startswith('PROGRESS:'):
                    progress = float(line.split(':')[1])
                    task_status['main_task']['progress'] = progress
                    continue
                # 日志仅追加，不参与进度计算
                task_status['main_task']['message'] = line
                logs = task_status['main_task'].setdefault('logs', [])
                logs.append(line)
                if len(logs) > 200:
                    logs.pop(0)

        main_process.wait()
        
        if main_stop_requested:
            task_status['main_task']['status'] = 'stopped'
            task_status['main_task']['message'] = '用户已停止新闻获取'
            add_run_history('main_task', None, None, 'stopped', task_status['main_task']['message'], None, completed=False)
        elif main_process.returncode == 0:
            task_status['main_task']['status'] = 'completed'
            task_status['main_task']['progress'] = 100
            task_status['main_task']['message'] = '新闻爬取完成'
            
            # 尝试读取最新的HTML文件作为预览
            today = datetime.now().strftime('%Y年%m月%d日')
            html_dir = OUTPUT_DIR / today / 'html'
            result_html = None
            if html_dir.exists():
                html_files = list(html_dir.glob('*.html'))
                if html_files:
                    latest_html = max(html_files, key=os.path.getctime)
                    with open(latest_html, 'r', encoding='utf-8') as f:
                        task_status['main_task']['result'] = f.read()
                    result_html = str(latest_html)
            add_run_history('main_task', None, None, 'completed', task_status['main_task']['message'], result_html)
        else:
            task_status['main_task']['status'] = 'error'
            task_status['main_task']['message'] = '执行失败: 请查看日志输出'
            add_run_history('main_task', None, None, 'error', task_status['main_task']['message'], None, completed=False)
            
    except Exception as e:
        task_status['main_task']['status'] = 'error'
        task_status['main_task']['message'] = f'执行出错: {str(e)}'
        add_run_history('main_task', None, None, 'error', task_status['main_task']['message'], None, completed=False)

def run_filter_script(model_type='deepseek', platform='siliconflow', api_token='', limit=5):
    """运行图片管线筛选脚本，支持平台参数与手动Token，以及可配置的AI筛选数量"""
    global task_status, filter_process, filter_stop_requested
    try:
        task_status['filter_task']['status'] = 'running'
        task_status['filter_task']['progress'] = 10
        task_status['filter_task']['message'] = f'正在启动AI新闻筛选（平台: {platform}，模型: {model_type}，数量: {limit}）...'
        task_status['filter_task']['logs'] = []
        if platform == 'volcengine' and model_type == 'qwen':
            task_status['filter_task']['message'] += '（提示：千问模型仅在硅基流动/阿里云可用）'

        # 改为调用整合版图片管线脚本
        script_name = 'news_filter_image_pipeline.py'

        if not api_token:
            task_status['filter_task']['status'] = 'error'
            task_status['filter_task']['message'] = '缺少API Token，请在页面为所选平台输入Token'
            add_run_history('filter_task', platform, model_type, 'error', task_status['filter_task']['message'], None, completed=False)
            return

        filter_stop_requested = False
        filter_process = subprocess.Popen(
            ['python', '-u', script_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='ignore',
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1', 'PYTHONUNBUFFERED': '1', 'WEB_APP': '1', 'AI_PLATFORM': platform, 'AI_MODEL_TYPE': model_type, 'AI_FILTER_LIMIT': str(limit), 'ARK_API_KEY': api_token},
            cwd=str(BASE_DIR)
        )

        task_status['filter_task']['message'] = 'AI正在分析和筛选新闻...'

        # 流式读取输出，展示在前端日志区
        for line in iter(filter_process.stdout.readline, ''):
            # 若收到停止请求，主动结束循环并尝试终止子进程
            if filter_stop_requested:
                try:
                    filter_process.terminate()
                except Exception:
                    pass
                break
            if not line:
                break
            line = line.strip()
            if line:
                # 支持脚本通过 "PROGRESS:" 前缀上报线性进度
                if line.startswith('PROGRESS:'):
                    try:
                        progress = float(line.split(':')[1])
                        task_status['filter_task']['progress'] = progress
                        # 进度日志不重复进入文本日志区
                        continue
                    except Exception:
                        pass
                task_status['filter_task']['message'] = line
                logs = task_status['filter_task'].setdefault('logs', [])
                logs.append(line)
                if len(logs) > 200:
                    logs.pop(0)

        filter_process.wait()

        if filter_stop_requested:
            task_status['filter_task']['status'] = 'stopped'
            task_status['filter_task']['message'] = '用户已停止AI筛选'
            add_run_history('filter_task', platform, model_type, 'stopped', task_status['filter_task']['message'], None, completed=False)
        elif filter_process.returncode == 0:
            task_status['filter_task']['status'] = 'completed'
            task_status['filter_task']['progress'] = 100
            task_status['filter_task']['message'] = 'AI筛选与图片抓取完成'

            today = datetime.now().strftime('%Y年%m月%d日')
            # 返回生成的图片页面路径（如果存在）
            result_file = OUTPUT_DIR / today / 'html' / '图片抓取结果_Bing.html'

            result_path = str(result_file) if result_file.exists() else None
            if result_file.exists():
                # 这里不读取整个HTML作为结果，只回传路径，前端会刷新结果并预览
                task_status['filter_task']['result'] = f'{result_path}'
            add_run_history('filter_task', platform, model_type, 'completed', task_status['filter_task']['message'], result_path)
        else:
            task_status['filter_task']['status'] = 'error'
            task_status['filter_task']['message'] = '执行失败: 请查看日志输出'
            add_run_history('filter_task', platform, model_type, 'error', task_status['filter_task']['message'], None, completed=False)
    except Exception as e:
        task_status['filter_task']['status'] = 'error'
        task_status['filter_task']['message'] = f'执行出错: {str(e)}'
        add_run_history('filter_task', platform, model_type, 'error', task_status['filter_task']['message'], None, completed=False)

@app.route('/')
def index():
    """主页面"""
    return render_template('index.html')

@app.route('/config')
def config_page():
    """配置页面"""
    return render_template('config.html')

@app.route('/api/run_main', methods=['POST'])
def api_run_main():
    """运行main.py的API接口"""
    if task_status['main_task']['status'] == 'running':
        return jsonify({'error': '任务正在运行中'}), 400
    
    # 重置任务状态
    task_status['main_task'] = {'status': 'idle', 'progress': 0, 'message': '', 'result': None}
    
    # 在新线程中运行任务
    thread = threading.Thread(target=run_main_script)
    thread.daemon = True
    thread.start()
    
    return jsonify({'message': '任务已启动'})

@app.route('/api/stop_main', methods=['POST'])
def api_stop_main():
    """停止main.py任务"""
    global main_process, main_stop_requested
    try:
        if task_status['main_task']['status'] != 'running' or main_process is None:
            return jsonify({'error': '任务未在运行'}), 400
        main_stop_requested = True
        try:
            main_process.terminate()
        except Exception:
            pass
        # 给予短暂时间让进程退出
        time.sleep(0.2)
        if main_process.poll() is None:
            try:
                main_process.kill()
            except Exception:
                pass
        # 若进程已退出，则直接标记为已停止
        if main_process.poll() is not None:
            task_status['main_task']['status'] = 'stopped'
            task_status['main_task']['message'] = '用户已停止新闻获取'
        else:
            task_status['main_task']['status'] = 'stopping'
            task_status['main_task']['message'] = '正在停止...'
        return jsonify({'message': '停止命令已发送'})
    except Exception as e:
        return jsonify({'error': f'停止失败: {e}'}), 500

@app.route('/api/run_filter', methods=['POST'])
def api_run_filter():
    """运行news_filter.py的API接口（仅使用手动输入Token）"""
    if task_status['filter_task']['status'] == 'running':
        return jsonify({'error': '任务正在运行中'}), 400

    data = request.get_json() or {}
    model_type = data.get('model_type', 'deepseek')
    platform = data.get('platform', 'siliconflow')
    api_token = (data.get('api_token') or '').strip()
    # 新增: 可配置AI筛选数量，默认5
    try:
        limit = int(data.get('limit', 5))
        if limit <= 0:
            limit = 5
    except Exception:
        limit = 5

    if not api_token:
        return jsonify({'error': '请为所选平台输入API Token'}), 400

    task_status['filter_task'] = {'status': 'idle', 'progress': 0, 'message': '', 'result': None, 'logs': []}

    # 为避免未完成时删除既有结果，取消启动前的输出清理。
    # 仅在任务成功完成后由生成流程进行安全替换，不在启动阶段删除任何文件或目录。

    thread = threading.Thread(target=run_filter_script, args=(model_type, platform, api_token, limit))
    thread.daemon = True
    thread.start()

    return jsonify({'message': '任务已启动'})

@app.route('/api/stop_filter', methods=['POST'])
def api_stop_filter():
    """停止筛选任务"""
    global filter_process, filter_stop_requested
    try:
        if task_status['filter_task']['status'] != 'running' or filter_process is None:
            return jsonify({'error': '任务未在运行'}), 400
        filter_stop_requested = True
        try:
            filter_process.terminate()
        except Exception:
            pass
        time.sleep(0.2)
        if filter_process.poll() is None:
            try:
                filter_process.kill()
            except Exception:
                pass
        if filter_process.poll() is not None:
            task_status['filter_task']['status'] = 'stopped'
            task_status['filter_task']['message'] = '用户已停止AI筛选'
        else:
            task_status['filter_task']['status'] = 'stopping'
            task_status['filter_task']['message'] = '正在停止...'
        return jsonify({'message': '停止命令已发送'})
    except Exception as e:
        return jsonify({'error': f'停止失败: {e}'}), 500

@app.route('/api/task_status/<task_name>')
def api_task_status(task_name):
    """获取任务状态"""
    if task_name in task_status:
        return jsonify(task_status[task_name])
    return jsonify({'error': '任务不存在'}), 404

@app.route('/api/config', methods=['GET'])
def api_get_config():
    """获取配置"""
    config = load_config()
    if config:
        return jsonify(config)
    return jsonify({'error': '配置加载失败'}), 500

@app.route('/api/config', methods=['POST'])
def api_save_config():
    """保存配置"""
    try:
        config = request.get_json()
        if save_config(config):
            return jsonify({'message': '配置保存成功'})
        else:
            return jsonify({'error': '配置保存失败'}), 500
    except Exception as e:
        return jsonify({'error': f'配置保存出错: {str(e)}'}), 500

# 平台选择接口：用于前端勾选/取消目标新闻平台
@app.route('/api/platforms', methods=['GET'])
def api_get_platforms():
    """返回所有平台与当前选中平台ID列表"""
    try:
        cfg = load_config() or {}
        all_list = load_all_platforms()
        supported_ids = {p['id'] for p in all_list}
        # 仅返回受支持的已选ID（剔除未知/失效渠道）
        selected = [p.get('id') for p in (cfg.get('platforms') or []) if isinstance(p, dict) and p.get('id') in supported_ids]
        return jsonify({'all': all_list, 'selected_ids': selected})
    except Exception as e:
        return jsonify({'error': f'平台列表获取失败: {str(e)}'}), 500

@app.route('/api/platforms', methods=['POST'])
def api_save_platforms():
    """保存前端提交的选中平台ID，写入配置的 platforms 列表"""
    try:
        data = request.get_json() or {}
        selected_ids = data.get('selected_ids') or []
        if not isinstance(selected_ids, list):
            return jsonify({'error': '参数格式错误: selected_ids 需要为数组'}), 400
        selected_ids = [str(x).strip() for x in selected_ids if str(x).strip()]
        # 仅保存受支持ID；名称来自模板映射
        id_to_name = {p['id']: p.get('name', p['id']) for p in load_all_platforms()}
        new_platforms = [{'id': pid, 'name': id_to_name[pid]} for pid in selected_ids if pid in id_to_name]
        ok = save_config({'platforms': new_platforms})
        if ok:
            return jsonify({'message': '平台选择已保存', 'count': len(new_platforms)})
        else:
            return jsonify({'error': '保存失败'}), 500
    except Exception as e:
        return jsonify({'error': f'保存平台选择出错: {str(e)}'}), 500

@app.route('/api/news_filter_config', methods=['GET'])
def api_get_news_filter_config():
    """获取 news_filter 配置（优先读取数据库，缺失时使用代码默认）"""
    try:
        base_config = {
            'models': [
                {'id': 'doubao', 'name': '豆包模型 (doubao-seed-1-6-flash-250828)'},
                {'id': 'deepseek', 'name': 'DeepSeek模型 (deepseek-v3-1-terminus)'},
                {'id': 'qwen', 'name': '千问模型 (Qwen/Qwen2.5-72B-Instruct)'}
            ],
            'platforms': [
                {'id': 'volcengine', 'name': '火山引擎 Ark'},
                {'id': 'siliconflow', 'name': '硅基流动'},
                {'id': 'aliyun', 'name': '阿里云百炼'}
            ],
            'current_model': 'deepseek',
            'limit': 5,
            'system_prompt': '你是一个专业的新闻筛选助手，专注于筛选社会民生类新闻，且输出适合作为视频话题、能够引发激烈讨论的内容。\n\n优先选择：\n- 与普通民众生活直接相关（教育、医疗、住房、就业、公共服务、消费维权）。\n- 政策新规及其影响（明确指出影响范围、人群、成本/价格变化）。\n- 具有争议点或明显讨论度的事件（如涨价/停供/校规冲突/物业纠纷/食品安全）。\n- 自然灾害与极端天气对生活的影响（供水供电供暖交通受阻等）。\n\n明确排除：\n- 面向程序员或开发者的技术文章、教程、框架方案。\n- 纯娱乐、明星八卦、赛事战报类内容。\n- 纯国际政治不贴近民生、缺少对居民实际影响的抽象报道。\n\n输出要求：\n- 返回JSON，仅包含 id、title、source、reason 字段。\n- reason 中请写清：争议点/讨论点、受影响人群、关键政策/数据、视频切入角度。',
            'user_keywords': '民生, 社会, 公共, 服务, 保障, 福利, 救助, 帮扶, 就业, 收入, 工资, 待遇, 生活, 居民, 市民, 群众, 教育, 学校, 学生, 老师, 教师, 学费, 奖学金, 助学金, 招生, 考试, 高考, 中考, 研究生, 博士, 硕士, 学历, 培训, 课程, 教学, 校园, 体罚, 霸凌, 安全, 医疗, 医院, 医生, 护士, 病人, 患者, 治疗, 手术, 药品, 疫苗, 健康, 疾病, 防疫, 公共卫生, 医保, 看病, 就医, 挂号, 检查, 诊断, 康复, 政策, 法规, 法律, 条例, 规定, 通知, 公告, 决定, 改革, 措施, 方案, 计划, 实施, 执行, 监管, 处罚, 政府, 部门, 机关, 公务员, 干部, 领导, 交通, 出行, 火车, 高铁, 地铁, 公交, 飞机, 机场, 车票, 票价, 免费, 优惠, 铁路, 公路, 航空, 港口, 拥堵, 限行, 停车, 违章, 事故, 住房, 房价, 租房, 买房, 公租房, 廉租房, 保障房, 物业, 小区, 社区, 邻里, 装修, 搬迁, 拆迁, 环境, 环保, 污染, 治理, 生态, 绿化, 空气, 水质, 垃圾, 回收, 节能, 减排, 气候, 天气, 治安, 犯罪, 案件, 警察, 公安, 消防, 救援, 灾害, 应急, 预警, 防范, 保护, 供水, 供电, 供暖, 供气, 食品, 菜价, 物价, 停车费, 物业费, 养老, 托育, 育儿',
            'exclude_keywords': '明星, 网红, 直播, 综艺, 娱乐, 八卦, 绯闻, 恋情, 结婚, 离婚, 出轨, 整容, 减肥, 时尚, 美妆, 足球, 篮球, 比赛, 联赛, 世界杯, 奥运会, 运动员, 球员, 教练, 转会, 赛事, 体育, 广告, 推广, 营销, 促销, 优惠券, 折扣, 特价, 限时, 代言, 品牌, 前端, 后端, 编程, 代码, 开发者, 工程师, 架构, 框架, 算法, 调试, SDK, API, JWT, OAuth, localStorage, Service Worker, TypeScript, JavaScript, Java, Python, Go, Rust, 数据库, 缓存, 微服务, Kubernetes, 容器, Git, GitHub, 开源, CI, CD, DevOps, 单元测试, 前沿技术, 技术方案, 技术栈, 技术总结, 技术分享, 源码',
            'keyword_specificity': 'balanced',
            'similarity_threshold': 0.7
        }
        db_conf = load_app_config() or {}
        # 使用数据库中的配置覆盖默认（仅在有有效值时覆盖）
        for key in [
            'system_prompt', 'user_keywords', 'exclude_keywords',
            'current_model', 'limit', 'keyword_specificity', 'similarity_threshold'
        ]:
            if key in db_conf:
                val = db_conf.get(key)
                if isinstance(val, list):
                    if len(val) > 0:
                        base_config[key] = val
                elif isinstance(val, str):
                    if val.strip():
                        base_config[key] = val
                elif val not in (None, ""):
                    base_config[key] = val
        return jsonify(base_config)
    except Exception as e:
        return jsonify({'error': f'配置读取失败: {str(e)}'}), 500

@app.route('/api/news_filter_config', methods=['POST'])
def api_save_news_filter_config():
    """保存news_filter配置（写入SQLite，不改动源码）"""
    try:
        cfg = request.get_json() or {}
        # 移除不再支持的字段
        if 'preferred_sources' in cfg:
            cfg.pop('preferred_sources', None)
        # 合并保存：避免局部更新导致覆盖与回退默认
        existing = load_app_config() or {}
        merged = dict(existing)
        for k, v in cfg.items():
            # 统一字符串去除两侧空格（保持与前端一致的格式）
            if isinstance(v, str):
                merged[k] = v.strip()
            else:
                merged[k] = v
        ok = save_app_config(merged)
        if ok:
            return jsonify({'message': '配置保存成功'})
        else:
            return jsonify({'error': '配置保存失败'}), 500
    except Exception as e:
        return jsonify({'error': f'配置保存出错: {str(e)}'}), 500

@app.route('/api/results')
def api_results():
    """获取结果文件列表"""
    print("收到 /api/results 请求")
    try:
        results = []
        output_dir = OUTPUT_DIR
        print(f"检查输出目录: {output_dir.absolute()}")
        if output_dir.exists():
            print(f"输出目录存在，开始扫描子目录...")
            for date_dir in output_dir.iterdir():
                if date_dir.is_dir():
                    print(f"处理日期目录: {date_dir.name}")
                    date_results = {'date': date_dir.name, 'files': []}
                    
                    # 查找HTML文件
                    html_dir = date_dir / 'html'
                    if html_dir.exists():
                        print(f"找到HTML目录: {html_dir}")
                        for html_file in html_dir.glob('*.html'):
                            print(f"找到HTML文件: {html_file.name}")
                            file_info = {
                                'name': html_file.name,
                                'type': 'html',
                                'path': str(html_file.relative_to(output_dir)),
                                'size': html_file.stat().st_size,
                                'modified': datetime.fromtimestamp(html_file.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                            }
                            date_results['files'].append(file_info)
                    
                    # 查找JSON文件
                    json_dir = date_dir / 'json'
                    if json_dir.exists():
                        for json_file in json_dir.glob('*.json'):
                            file_info = {
                                'name': json_file.name,
                                'type': 'json',
                                'path': str(json_file.relative_to(output_dir)),
                                'size': json_file.stat().st_size,
                                'modified': datetime.fromtimestamp(json_file.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                            }
                            date_results['files'].append(file_info)
                    
                    # 查找筛选结果文件
                    for txt_file in date_dir.glob('*.txt'):
                        file_info = {
                            'name': txt_file.name,
                            'type': 'txt',
                            'path': str(txt_file.relative_to(output_dir)),
                            'size': txt_file.stat().st_size,
                            'modified': datetime.fromtimestamp(txt_file.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                        }
                        date_results['files'].append(file_info)
                    
                    if date_results['files']:
                        results.append(date_results)
        
        return jsonify({'results': results})
    except Exception as e:
        return jsonify({'error': f'获取结果列表失败: {str(e)}'}), 500

@app.route('/api/download/<path:file_path>')
def api_download(file_path):
    """下载结果文件"""
    try:
        full_path = OUTPUT_DIR / file_path
        if not full_path.exists() or not full_path.is_file():
            return jsonify({'error': '文件不存在'}), 404
        
        # 安全检查：确保文件在output目录内
        if not str(full_path.resolve()).startswith(str(OUTPUT_DIR.resolve())):
            return jsonify({'error': '非法文件路径'}), 403
        
        return send_from_directory(
            str(full_path.parent),
            full_path.name,
            as_attachment=True
        )
    except Exception as e:
        return jsonify({'error': f'下载失败: {str(e)}'}), 500

@app.route('/api/preview/<path:file_path>')
def api_preview(file_path):
    """预览结果文件内容"""
    try:
        # URL解码文件路径
        from urllib.parse import unquote
        decoded_path = unquote(file_path)
        
        # 调试信息
        print(f"原始文件路径: {file_path}")
        print(f"解码后路径: {decoded_path}")
        
        # 规范化路径分隔符
        normalized_path = decoded_path.replace('/', os.sep)
        full_path = OUTPUT_DIR / normalized_path
        
        print(f"规范化路径: {normalized_path}")
        print(f"完整路径: {full_path}")
        print(f"文件是否存在: {full_path.exists()}")
        
        if not full_path.exists() or not full_path.is_file():
            return jsonify({'error': f'文件不存在: {full_path}'}), 404
        
        # 安全检查：确保文件在output目录内
        output_resolved = OUTPUT_DIR.resolve()
        file_resolved = full_path.resolve()
        if not str(file_resolved).startswith(str(output_resolved)):
            return jsonify({'error': '非法文件路径'}), 403
        
        # 读取文件内容
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except UnicodeDecodeError:
            # 如果UTF-8解码失败，尝试其他编码
            with open(full_path, 'r', encoding='gbk') as f:
                content = f.read()
        
        return jsonify({
            'content': content,
            'type': full_path.suffix.lower(),
            'name': full_path.name,
            'size': full_path.stat().st_size
        })
    except Exception as e:
        print(f"预览失败详细错误: {str(e)}")
        return jsonify({'error': f'预览失败: {str(e)}'}), 500

@app.route('/view/latest')
def view_latest():
    """无文件化预览：直接返回最新一次新闻获取的 HTML 页面内容。
    优先使用内存中的 main_task 结果；如不存在则读取最新 HTML 文件。
    """
    try:
        # 优先使用内存结果
        html_content = task_status.get('main_task', {}).get('result')
        if not html_content:
            # 若上述为空则读取 output/当天/html 下最新文件
            today = datetime.now().strftime('%Y年%m月%d日')
            html_dir = OUTPUT_DIR / today / 'html'
            if html_dir.exists():
                html_files = list(html_dir.glob('*.html'))
                if html_files:
                    latest_html = max(html_files, key=os.path.getctime)
                    with open(latest_html, 'r', encoding='utf-8', errors='ignore') as f:
                        html_content = f.read()

        if not html_content:
            return jsonify({'error': '暂无可预览内容，请先运行新闻获取任务'}), 404

        return Response(html_content, mimetype='text/html; charset=utf-8')
    except Exception as e:
        return jsonify({'error': f'生成预览失败: {str(e)}'}), 500

@app.route('/view/file/<path:file_path>')
def view_file(file_path):
    """直接以网页形式打开指定输出目录中的文件（主要用于HTML）。
    安全限制：仅允许访问 OUTPUT_DIR 下的文件路径。
    """
    try:
        from urllib.parse import unquote
        decoded_path = unquote(file_path)
        normalized_path = decoded_path.replace('/', os.sep)
        full_path = OUTPUT_DIR / normalized_path

        if not full_path.exists() or not full_path.is_file():
            return jsonify({'error': f'文件不存在: {full_path}'}), 404

        # 安全检查：确保文件在output目录内
        output_resolved = OUTPUT_DIR.resolve()
        file_resolved = full_path.resolve()
        if not str(file_resolved).startswith(str(output_resolved)):
            return jsonify({'error': '非法文件路径'}), 403

        # 读取文件内容并根据类型返回
        suffix = full_path.suffix.lower()
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except UnicodeDecodeError:
            with open(full_path, 'r', encoding='gbk') as f:
                content = f.read()

        if suffix == '.html':
            return Response(content, mimetype='text/html; charset=utf-8')
        elif suffix in ('.json', '.txt', '.md'):
            return Response(content, mimetype='text/plain; charset=utf-8')
        else:
            # 其他类型，尝试作为文本返回
            return Response(content, mimetype='text/plain; charset=utf-8')
    except Exception as e:
        return jsonify({'error': f'文件预览失败: {str(e)}'}), 500

@app.route('/api/test_model', methods=['POST'])
def api_test_model():
    """测试模型可用性：严格要求页面提供Token，不再读取环境变量"""
    try:
        data = request.get_json() or {}
        platform = data.get('platform', 'volcengine')
        model_type = (data.get('model_type') or 'doubao').lower()
        api_token = (data.get('api_token') or '').strip()

        if not api_token:
            return jsonify({'ok': False, 'error': '请在页面为所选平台输入API Token'}), 400

        # 初始化客户端与模型映射（仅使用页面传入的Token）
        if platform == 'siliconflow':
            client = _OpenAIClient(api_key=api_token, base_url='https://api.siliconflow.cn/v1')
            model_map = {
                'doubao': 'Qwen/Qwen2.5-72B-Instruct',
                'deepseek': 'Pro/deepseek-ai/DeepSeek-R1',
                'qwen': 'Qwen/Qwen2.5-72B-Instruct',
            }
        elif platform == 'aliyun':
            client = _OpenAIClient(api_key=api_token, base_url='https://dashscope.aliyuncs.com/compatible-mode/v1')
            model_map = {
                'doubao': 'qwen-plus',
                'deepseek': 'deepseek-r1',
                'qwen': 'qwen-plus',
            }
        else:
            client = _OpenAIClient(base_url=ARK_API_ENDPOINT, api_key=api_token)
            model_map = {
                'doubao': ARK_DOUHAO_MODEL,
                'deepseek': 'deepseek-v3-1-terminus',
            }

        model_id = model_map.get(model_type, model_type)

        # 发送最小测试消息
        if platform in ('siliconflow', 'aliyun'):
            resp = client.chat.completions.create(
                model=model_id,
                messages=[
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "你是谁？"},
                ],
            )
            # 解析文本
            text = None
            try:
                if hasattr(resp, 'choices') and resp.choices:
                    msg = resp.choices[0].message
                    if msg and hasattr(msg, 'content'):
                        text = msg.content
            except Exception:
                text = None
            return jsonify({'ok': True, 'platform': platform, 'model': model_id, 'preview': (text or '')[:200]})
        else:
            resp = client.chat.completions.create(
                model=model_id,
                messages=[
                    {"role": "system", "content": [{"type": "text", "text": "You are a helpful assistant."}]},
                    {"role": "user", "content": [{"type": "text", "text": "你是谁？"}]},
                ],
                extra_headers={"x-is-encrypted": "true"},
            )
            # 解析 Ark 文本
            text = None
            try:
                choices = getattr(resp, 'choices', [])
                if choices:
                    msg = getattr(choices[0], 'message', None)
                    if msg is not None:
                        cont = getattr(msg, 'content', None)
                        if isinstance(cont, list) and cont:
                            first_block = cont[0]
                            if isinstance(first_block, dict) and 'text' in first_block:
                                text = first_block['text']
            except Exception:
                text = None
            return jsonify({'ok': True, 'platform': platform, 'model': model_id, 'preview': (text or '')[:200]})
    except Exception as e:
        return jsonify({'ok': False, 'error': f'测试失败: {str(e)}'}), 400

@app.route('/api/clear_results', methods=['POST'])
def api_clear_results():
    """清空所有结果文件"""
    try:
        import shutil
        output_dir = OUTPUT_DIR
        if output_dir.exists():
            # 删除output目录下的所有内容，但保留.gitkeep文件
            for item in output_dir.iterdir():
                if item.name != '.gitkeep':
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
        
        return jsonify({'message': '结果文件已清空'})
    except Exception as e:
        return jsonify({'error': f'清空失败: {str(e)}'}), 500


# Coze 工作流视频生成：后端转发
@app.route('/api/coze/workflow_run', methods=['POST'])
def api_coze_workflow_run():
    try:
        body = request.get_json() or {}
        # 兼容插件与网页端多种字段：images/image_urls
        images = body.get('images') or body.get('image_urls') or []
        article = body.get('article') or {}
        duration = body.get('duration_per_image', 2)

        # 文章URL列表
        article_urls = []
        if isinstance(article, dict):
            url = article.get('url')
            if url:
                article_urls = [url]
        elif isinstance(article, list):
            for it in article:
                if isinstance(it, dict) and it.get('url'):
                    article_urls.append(it['url'])
                elif isinstance(it, str) and it.strip():
                    article_urls.append(it.strip())
        elif isinstance(article, str) and article.strip():
            article_urls = [article.strip()]

        # 从请求体优先读取凭据；若未提供则回退到配置
        conf = load_config() or {}
        wf = (conf.get('coze') or {}).get('workflow', {})
        workflow_id = body.get('workflow_id') or body.get('bot_id') or wf.get('bot_id') or wf.get('workflow_id')
        api_token = body.get('api_token') or body.get('token') or wf.get('api_token')

        if not workflow_id or not api_token:
            return jsonify({'error': '缺少 Workflow ID 或 Token：请在请求体提供或在配置页填写'}), 400

        payload = {
            'workflow_id': workflow_id,
            'parameters': {
                'images': images,
                'url': article_urls,
                'duration_per_image': duration
            }
        }

        headers = {
            'Authorization': f'Bearer {api_token}',
            'Content-Type': 'application/json'
        }

        # 提高视频生成请求的读超时到 600 秒（连接超时 10 秒，读超时 600 秒）
        resp = requests.post('https://api.coze.cn/v1/workflow/run', json=payload, headers=headers, timeout=(100, 600))
        if not resp.ok:
            return jsonify({'error': f'Coze API请求失败: {resp.status_code} {resp.reason}', 'detail': resp.text}), resp.status_code

        data = resp.json()
        # 可能返回 {code, msg, data}
        if isinstance(data, dict) and 'code' in data and data.get('code') not in (0, None):
            return jsonify({'error': data.get('msg') or 'Coze返回错误', 'raw': data}), 400

        download_url = ''
        d = data.get('data') if isinstance(data, dict) else data

        def extract_url(text: str) -> str:
            m = re.search(r'(https?://[^\s\"\`]+)', text or '')
            return m.group(1) if m else ''

        if isinstance(d, str):
            # 字符串化的JSON或包含链接的文本
            try:
                j = json.loads(d)
                out = j.get('output')
                if isinstance(out, str):
                    download_url = extract_url(out)
            except Exception:
                download_url = extract_url(d)
        elif isinstance(d, dict):
            out = d.get('output')
            if isinstance(out, str):
                download_url = extract_url(out)

        if not download_url:
            # 回退：尝试在整个响应中寻找URL
            try:
                download_url = extract_url(json.dumps(data, ensure_ascii=False))
            except Exception:
                pass

        if not download_url:
            return jsonify({'error': '未在响应中找到下载URL', 'raw': data}), 400

        return jsonify({'download_url': download_url, 'raw': data})
    except Exception as e:
        return jsonify({'error': f'调用Coze失败: {str(e)}'}), 500


# Coze Chat 文案生成：复现插件“新闻采集与创作”调用
@app.route('/api/coze/chat_generate', methods=['POST'])
def api_coze_chat_generate():
    try:
        body = request.get_json() or {}
        # 接收与插件一致的三段输入
        input_prompt = body.get('input_prompt') or ''
        # 兼容插件：可直接传入 url/title，或 input_article
        input_article = body.get('input_article') or ''
        url = body.get('url')
        title = body.get('title')
        # 若提供了 URL，则仅使用链接（不包含标题）
        if url and isinstance(url, str):
            input_article = url.strip()
        # 兼容图片数组字段：images/image_urls 或 input_video
        input_video = body.get('input_video') or body.get('images') or body.get('image_urls') or []

        # 兼容列表输入，将文章数组拼接为插件默认的字符串形式（\n\n分隔）
        if isinstance(input_article, list):
            input_article = '\n\n'.join([str(x) for x in input_article if x])

        if not isinstance(input_video, list):
            input_video = []

        # 从请求体优先读取凭据；若未提供则回退到配置
        conf = load_config() or {}
        chat_conf = (conf.get('coze') or {}).get('chat', {})
        bot_id = body.get('bot_id') or chat_conf.get('bot_id')
        api_token = body.get('api_token') or body.get('token') or chat_conf.get('api_token')
        if not bot_id or not api_token:
            return jsonify({'error': '缺少 Chat Bot ID 或 Token：请在请求体提供或在配置页填写'}), 400

        # 构造与插件一致的请求体
        input_data = {
            'input_prompt': input_prompt,
            'input_article': input_article,
            'input_video': input_video
        }

        payload = {
            'bot_id': bot_id,
            'user_id': '123456789',  # 复现插件默认 user_id
            'stream': True,  # 使用流式以获取最终内容
            'additional_messages': [{
                'role': 'user',
                'content': json.dumps({'_input': input_data}, ensure_ascii=False),
                'content_type': 'text'
            }]
        }

        headers = {
            'Authorization': f'Bearer {api_token}',
            'Content-Type': 'application/json',
            'Accept': 'text/event-stream'
        }

        # 以流式方式读取 SSE 事件并聚合内容
        resp = requests.post('https://api.coze.cn/v3/chat', headers=headers, json=payload, timeout=(100, 600), stream=True)
        if not resp.ok:
            return jsonify({'error': f'Coze API请求失败: {resp.status_code} {resp.reason}', 'detail': resp.text}), resp.status_code

        result = ''
        current_event = None
        raw_events = []
        try:
            # 明确按UTF-8解码，避免requests错误推断编码导致中文乱码
            for raw_line in resp.iter_lines(decode_unicode=False):
                if raw_line is None:
                    continue
                # raw_line为bytes
                try:
                    line = raw_line.decode('utf-8', errors='replace').strip()
                except Exception:
                    line = str(raw_line).strip()
                if not line:
                    continue
                # 记录原始事件行（便于调试）
                raw_events.append(line)
                if line.startswith('event:'):
                    current_event = line[6:].strip()
                    continue
                if line.startswith('data:'):
                    data_str = line[5:].strip()
                    if data_str == '[DONE]':
                        break
                    # 尝试解析 JSON 数据
                    try:
                        block = json.loads(data_str)
                    except Exception:
                        # 非JSON数据块，跳过
                        continue
                    # 根据事件类型处理
                    if current_event == 'conversation.message.delta':
                        if isinstance(block, dict) and block.get('content'):
                            result += str(block['content'])
                    elif current_event == 'conversation.message.completed':
                        # 可忽略，最终内容以累积文本为准
                        pass
                    elif current_event == 'conversation.chat.completed':
                        # 会话完成，结束解析
                        break
                    elif current_event == 'error':
                        # 返回错误信息
                        msg = (block.get('msg') if isinstance(block, dict) else None) or '未知错误'
                        return jsonify({'error': f'Coze返回错误: {msg}', 'raw': block}), 400
                else:
                    # 非SSE格式，可能是直接JSON响应；作为兜底解析
                    try:
                        direct = json.loads(line)
                        if isinstance(direct, dict) and isinstance(direct.get('data'), dict):
                            content = direct['data'].get('content') or ''
                            if not content and isinstance(direct['data'].get('messages'), list):
                                answers = [m for m in direct['data']['messages'] if m.get('type') == 'answer']
                                if answers:
                                    content = answers[-1].get('content') or ''
                            if content:
                                result = content
                                break
                    except Exception:
                        # 不是JSON，忽略
                        pass

        except Exception as e:
            # 若流式解析失败，尝试回退为普通 JSON 解析
            try:
                data = resp.json()
                content = ''
                if isinstance(data.get('data'), dict):
                    content = data['data'].get('content') or ''
                    if not content and isinstance(data['data'].get('messages'), list):
                        answers = [m for m in data['data']['messages'] if m.get('type') == 'answer']
                        if answers:
                            content = answers[-1].get('content') or ''
                result = content
                raw_events.append({'fallback_json': data})
            except Exception:
                return jsonify({'error': f'返回解析失败: {str(e)}', 'raw': resp.text}), 500

        resp_json = jsonify({'content': result, 'raw': {'events': raw_events}})
        # 明确响应编码为UTF-8
        resp_json.headers['Content-Type'] = 'application/json; charset=utf-8'
        return resp_json
    except Exception as e:
        return jsonify({'error': f'调用Coze失败: {str(e)}'}), 500


# === 插件风格的简化接口：直接接收链接生成文章/视频 ===

# 从文章链接生成文章（等价于插件 callCozeAPI 的输入）
@app.route('/api/plugin/article_generate', methods=['POST'])
def api_plugin_article_generate():
    try:
        body = request.get_json() or {}
        # 重用 chat_generate 逻辑：允许在请求体中覆盖 bot_id/token
        # 构造 input_article 与 input_video
        url = body.get('url')
        title = body.get('title')
        images = body.get('images') or body.get('image_urls') or []
        if url and isinstance(url, str):
            # 仅传递链接，不附带标题
            input_article = url.strip()
        else:
            input_article = body.get('input_article') or ''

        # 读取或覆盖凭据
        conf = load_config() or {}
        chat_conf = (conf.get('coze') or {}).get('chat', {})
        bot_id = body.get('bot_id') or chat_conf.get('bot_id')
        api_token = body.get('api_token') or body.get('token') or chat_conf.get('api_token')
        if not bot_id or not api_token:
            return jsonify({'error': '缺少 Chat Bot ID 或 Token：请在请求体提供或在配置页填写'}), 400

        input_data = {
            'input_prompt': body.get('input_prompt') or '',
            'input_article': input_article,
            # 文章生成不包含图片素材
            'input_video': []
        }

        payload = {
            'bot_id': bot_id,
            'user_id': '123456789',
            'stream': True,
            'additional_messages': [{
                'content': json.dumps({'_input': input_data}, ensure_ascii=False),
                'content_type': 'text',
                'role': 'user'
            }]
        }
        headers = {
            'Authorization': f'Bearer {api_token}',
            'Content-Type': 'application/json',
            'Accept': 'text/event-stream'
        }
        resp = requests.post('https://api.coze.cn/v3/chat', headers=headers, json=payload, timeout=(100, 600), stream=True)
        if not resp.ok:
            return jsonify({'error': f'Coze API请求失败: {resp.status_code} {resp.reason}', 'detail': resp.text}), resp.status_code

        result = ''
        current_event = None
        raw_events = []
        try:
            for raw_line in resp.iter_lines(decode_unicode=False):
                if raw_line is None:
                    continue
                try:
                    line = raw_line.decode('utf-8', errors='replace').strip()
                except Exception:
                    line = str(raw_line).strip()
                if not line:
                    continue
                raw_events.append(line)
                if line.startswith('event:'):
                    current_event = line[6:].strip()
                    continue
                if line.startswith('data:'):
                    data_str = line[5:].strip()
                    if data_str == '[DONE]':
                        break
                    try:
                        block = json.loads(data_str)
                    except Exception:
                        continue
                    if current_event == 'conversation.message.delta':
                        if isinstance(block, dict) and block.get('content'):
                            result += str(block['content'])
                    elif current_event == 'conversation.chat.completed':
                        break
                    elif current_event == 'error':
                        msg = (block.get('msg') if isinstance(block, dict) else None) or '未知错误'
                        return jsonify({'error': f'Coze返回错误: {msg}', 'raw': block}), 400
                else:
                    try:
                        direct = json.loads(line)
                        if isinstance(direct, dict) and isinstance(direct.get('data'), dict):
                            content = direct['data'].get('content') or ''
                            if not content and isinstance(direct['data'].get('messages'), list):
                                answers = [m for m in direct['data']['messages'] if m.get('type') == 'answer']
                                if answers:
                                    content = answers[-1].get('content') or ''
                            if content:
                                result = content
                                break
                    except Exception:
                        pass
        except Exception as e:
            try:
                data = resp.json()
                content = ''
                if isinstance(data.get('data'), dict):
                    content = data['data'].get('content') or ''
                    if not content and isinstance(data['data'].get('messages'), list):
                        answers = [m for m in data['data']['messages'] if m.get('type') == 'answer']
                        if answers:
                            content = answers[-1].get('content') or ''
                result = content
                raw_events.append({'fallback_json': data})
            except Exception:
                return jsonify({'error': f'返回解析失败: {str(e)}', 'raw': resp.text}), 500

        resp_json = jsonify({'content': result, 'raw': {'events': raw_events}})
        resp_json.headers['Content-Type'] = 'application/json; charset=utf-8'
        return resp_json
    except Exception as e:
        return jsonify({'error': f'调用Coze失败: {str(e)}'}), 500


# 从文章+图片链接生成视频（等价于插件 callVideoCozeAPI 的输入）
@app.route('/api/plugin/video_generate', methods=['POST'])
def api_plugin_video_generate():
    try:
        body = request.get_json() or {}
        images = body.get('images') or body.get('image_urls') or []
        articles = body.get('articles') or body.get('article_urls') or []
        duration = body.get('duration_per_image', 2)

        # 统一为字符串数组
        image_urls = []
        for it in images:
            if isinstance(it, str) and it.strip():
                image_urls.append(it.strip())
            elif isinstance(it, dict):
                u = it.get('url') or it.get('src')
                if u:
                    image_urls.append(str(u).strip())

        article_urls = []
        for a in articles:
            if isinstance(a, str) and a.strip():
                article_urls.append(a.strip())
            elif isinstance(a, dict) and a.get('url'):
                article_urls.append(str(a['url']).strip())

        # 读取或覆盖凭据（workflow_id/bot_id 与 token）
        conf = load_config() or {}
        wf_conf = (conf.get('coze') or {}).get('workflow', {})
        workflow_id = body.get('workflow_id') or body.get('bot_id') or wf_conf.get('bot_id') or wf_conf.get('workflow_id')
        api_token = body.get('api_token') or body.get('token') or wf_conf.get('api_token')
        if not workflow_id or not api_token:
            return jsonify({'error': '缺少 Workflow ID 或 Token：请在请求体提供或在配置页填写'}), 400

        payload = {
            'workflow_id': workflow_id,
            'parameters': {
                'images': image_urls,
                'url': article_urls,
                'duration_per_image': duration
            }
        }
        headers = {
            'Authorization': f'Bearer {api_token}',
            'Content-Type': 'application/json',
            'Accept': '*/*'
        }
        resp = requests.post('https://api.coze.cn/v1/workflow/run', json=payload, headers=headers, timeout=(10, 600))
        if not resp.ok:
            return jsonify({'error': f'Coze API请求失败: {resp.status_code} {resp.reason}', 'detail': resp.text}), resp.status_code
        data = resp.json()
        d = data.get('data') if isinstance(data, dict) else data

        def extract_url(text: str) -> str:
            m = re.search(r'(https?://[^\s\"\`]+)', text or '')
            return m.group(1) if m else ''

        download_url = ''
        if isinstance(d, str):
            try:
                j = json.loads(d)
                out = j.get('output')
                if isinstance(out, str):
                    download_url = extract_url(out)
            except Exception:
                download_url = extract_url(d)
        elif isinstance(d, dict):
            out = d.get('output')
            if isinstance(out, str):
                download_url = extract_url(out)
        if not download_url:
            try:
                download_url = extract_url(json.dumps(data, ensure_ascii=False))
            except Exception:
                pass
        if not download_url:
            return jsonify({'error': '未在响应中找到下载URL', 'raw': data}), 400
        return jsonify({'download_url': download_url, 'raw': data})
    except Exception as e:
        return jsonify({'error': f'调用Coze失败: {str(e)}'}), 500

# Coze Chat 配置连通性测试
@app.route('/api/coze/test_chat', methods=['POST'])
def api_coze_test_chat():
    try:
        body = request.get_json() or {}
        # 允许从请求体覆盖配置页的当前输入
        conf = load_config() or {}
        chat_conf = (conf.get('coze') or {}).get('chat', {})
        bot_id = body.get('bot_id') or chat_conf.get('bot_id')
        api_token = body.get('api_token') or chat_conf.get('api_token')
        if not bot_id or not api_token:
            return jsonify({'error': '缺少 Chat Bot ID 或 Token'}), 400

        payload = {
            'bot_id': bot_id,
            'user_id': '123456789',
            'stream': False,
            'additional_messages': [{
                'content': 'ping',
                'content_type': 'text',
                'role': 'user'
            }]
        }
        headers = {
            'Authorization': f'Bearer {api_token}',
            'Content-Type': 'application/json'
        }
        resp = requests.post('https://api.coze.cn/v3/chat', headers=headers, json=payload, timeout=(10, 10))
        if not resp.ok:
            return jsonify({'error': f'Coze Chat 连接失败: {resp.status_code} {resp.reason}', 'detail': resp.text}), resp.status_code
        try:
            data = resp.json()
        except Exception:
            return jsonify({'error': '返回解析失败', 'raw': resp.text}), 500
        if isinstance(data, dict) and 'code' in data and data.get('code') not in (0, None):
            return jsonify({'error': data.get('msg') or 'Coze返回错误', 'raw': data}), 400
        return jsonify({'ok': True, 'message': 'Chat 配置可用'})
    except Exception as e:
        return jsonify({'error': f'测试失败: {str(e)}'}), 500

# Coze Workflow 配置连通性测试
@app.route('/api/coze/test_workflow', methods=['POST'])
def api_coze_test_workflow():
    try:
        body = request.get_json() or {}
        conf = load_config() or {}
        wf_conf = (conf.get('coze') or {}).get('workflow', {})
        workflow_id = body.get('workflow_id') or body.get('bot_id') or wf_conf.get('bot_id') or wf_conf.get('workflow_id')
        api_token = body.get('api_token') or wf_conf.get('api_token')
        if not workflow_id or not api_token:
            return jsonify({'error': '缺少 Workflow ID 或 Token'}), 400

        payload = {
            'workflow_id': workflow_id,
            'parameters': {
                'images': ['https://picsum.photos/320'],
                'url': ['https://example.com/'],
                'duration_per_image': 1
            }
        }
        headers = {
            'Authorization': f'Bearer {api_token}',
            'Content-Type': 'application/json',
            'Accept': '*/*'
        }
        # 测试请求使用更充裕的读取超时，避免因工作流初始化慢导致解析失败
        resp = requests.post('https://api.coze.cn/v1/workflow/run', json=payload, headers=headers, timeout=(10, 60))
        if not resp.ok:
            status = resp.status_code
            msg = f'Coze Workflow 连接失败: {status} {resp.reason}'
            return jsonify({'error': msg, 'detail': resp.text}), status
        try:
            data = resp.json()
        except Exception:
            # 返回原始文本帮助排查（例如参数签名不匹配、权限不足等）
            return jsonify({'ok': True, 'message': 'Workflow 凭据可用（返回非JSON）', 'raw': resp.text})
        if isinstance(data, dict) and 'code' in data and data.get('code') not in (0, None):
            # code非0通常表示参数或ID问题，但token已通过鉴权
            return jsonify({'error': data.get('msg') or 'Coze返回错误', 'raw': data}), 400
        return jsonify({'ok': True, 'message': 'Workflow 凭据可用'})
    except Exception as e:
        return jsonify({'error': f'测试失败: {str(e)}'}), 500

if __name__ == '__main__':
    # 确保templates和static目录存在
    os.makedirs(BASE_DIR / 'templates', exist_ok=True)
    os.makedirs(BASE_DIR / 'static', exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    port = int(os.environ.get('PORT', '5000'))

    # 定时循环执行：状态与接口
    scheduler_state = {
        'enabled': False,
        'interval_minutes': 30,
        'thread': None,
        'stop_event': None,
        'next_run': None,
    }

    def scheduler_loop():
        state = scheduler_state
        while state['enabled'] and state.get('stop_event') and not state['stop_event'].is_set():
            # 等待到下次运行时间
            interval = int(state.get('interval_minutes') or 30)
            state['next_run'] = (datetime.now() + timedelta(minutes=interval)).strftime('%Y-%m-%d %H:%M:%S')
            for _ in range(interval * 60):
                if state['stop_event'].is_set():
                    return
                time.sleep(1)
            # 到点后执行一次：先爬取，再AI筛选
            if task_status['main_task']['status'] != 'running' and task_status['filter_task']['status'] != 'running':
                try:
                    run_main_script()
                except Exception:
                    pass
                # 读取配置以决定平台、模型与limit
                conf = load_config() or {}
                platform = conf.get('platform', 'volcengine')
                tokens = (conf.get('tokens') or {})
                api_token = tokens.get(platform) or ''
                nf_conf = load_app_config() or {}
                model_type = nf_conf.get('current_model', 'doubao')
                try:
                    limit = int(nf_conf.get('limit', 5))
                except Exception:
                    limit = 5
                if api_token:
                    try:
                        run_filter_script(model_type, platform, api_token, limit)
                    except Exception:
                        pass

    @app.route('/api/schedule/start', methods=['POST'])
    def api_schedule_start():
        data = request.get_json() or {}
        try:
            interval = int(data.get('interval_minutes', 30))
            if interval <= 0:
                interval = 30
        except Exception:
            interval = 30
        # 停掉旧的
        if scheduler_state.get('thread') and scheduler_state['thread'].is_alive():
            if scheduler_state.get('stop_event'):
                scheduler_state['stop_event'].set()
                try:
                    scheduler_state['thread'].join(timeout=1)
                except Exception:
                    pass
        # 启动新的
        scheduler_state['enabled'] = True
        scheduler_state['interval_minutes'] = interval
        scheduler_state['stop_event'] = threading.Event()
        scheduler_state['thread'] = threading.Thread(target=scheduler_loop, daemon=True)
        scheduler_state['thread'].start()
        return jsonify({'message': '定时任务已启动', 'interval_minutes': interval})

    @app.route('/api/schedule/stop', methods=['POST'])
    def api_schedule_stop():
        if scheduler_state.get('stop_event'):
            scheduler_state['stop_event'].set()
        scheduler_state['enabled'] = False
        return jsonify({'message': '定时任务已停止'})

    @app.route('/api/schedule/status', methods=['GET'])
    def api_schedule_status():
        return jsonify({
            'enabled': scheduler_state.get('enabled', False),
            'interval_minutes': scheduler_state.get('interval_minutes', 30),
            'next_run': scheduler_state.get('next_run'),
        })

    app.run(debug=True, host='0.0.0.0', port=port)