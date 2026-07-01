#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
合并版：AI筛选 + 关键词提取 + Bing 图片抓取

目标：一次运行直接生成最终页面，不产生中间 txt 或筛选结果 html 文件。

用法：
  python -u news_filter_image_pipeline.py

说明：
- 内置 Ark Token 加载与关键词提取（OpenAI 兼容接口）
- 每条新闻提取最多 3 个关键词；每关键词抓取 10 张图片
- 输出固定文件名：output/当天/html/图片抓取结果_Bing.html（覆盖写入）
"""

import os
import re
import json
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional
from difflib import SequenceMatcher

import requests
from bs4 import BeautifulSoup
from openai import OpenAI
import subprocess


# ---- 模型与平台设置（统一依赖网页端选择；默认硅基流动 DeepSeek-V3） ----
ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
# 关键词提取的模型回退：与筛选统一，默认使用 SiliconFlow 的 DeepSeek-V3
LLM_MODEL_KEYWORD_EXTRACTION = "deepseek-ai/DeepSeek-V3"

# 平台与模型映射（OpenAI兼容接口）
PLATFORM_BASE_URLS = {
    'volcengine': ARK_BASE_URL,
    'siliconflow': 'https://api.siliconflow.cn/v1',
    # 阿里云百炼（DashScope）OpenAI兼容模式端点
    'aliyun': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
}

MODEL_ID_MAP = {
    'volcengine': {
        'doubao': 'doubao-seed-1-6-flash-250828',
        'deepseek': 'deepseek-v3-1-terminus',
        # 火山引擎不支持千问，使用豆包替代
        'qwen': 'doubao-seed-1-6-flash-250828',
    },
    'siliconflow': {
        'deepseek': 'deepseek-ai/DeepSeek-V3',
        'qwen': 'Qwen/Qwen2.5-72B-Instruct',
        # 硅基流动不支持豆包，使用 DeepSeek 替代
        'doubao': 'deepseek-ai/DeepSeek-V3',
    },
    'aliyun': {
        # DashScope 兼容模式常用命名（通用替代顺序）
        # 说明：不同账号可用的具体型号可能不同，优先使用广泛可用的别名
        'qwen': 'qwen-plus',
        'deepseek': 'qwen-plus',  # 阿里云不支持 deepseek，使用 qwen 系列
        'doubao': 'qwen-plus',    # 阿里云不支持 doubao，使用 qwen 系列
    },
}

def resolve_llm_settings() -> Dict[str, str]:
    """从环境变量解析平台与模型，并映射为具体 base_url 与 model_id；同时加载可用的Token。
    若页面/环境未设置，则默认 platform='siliconflow'、model_type='deepseek'。
    """
    platform = (os.environ.get('AI_PLATFORM') or 'siliconflow').strip().lower()
    model_type = (os.environ.get('AI_MODEL_TYPE') or 'deepseek').strip().lower()
    base_url = PLATFORM_BASE_URLS.get(platform, ARK_BASE_URL)
    model_id = MODEL_ID_MAP.get(platform, {}).get(model_type) or LLM_MODEL_KEYWORD_EXTRACTION
    token = load_local_token(platform)
    return {
        'platform': platform,
        'model_type': model_type,
        'base_url': base_url,
        'model_id': model_id,
        'token': token,
    }


def load_local_token(platform: str = "volcengine") -> str:
    """加载平台API Token：优先 config/config.yaml，其次环境变量（含平台回退）。"""
    try:
        config_path = Path(__file__).parent / 'config' / 'config.yaml'
        if config_path.exists():
            import yaml
            with open(config_path, 'r', encoding='utf-8') as f:
                cfg = yaml.safe_load(f) or {}
            tokens = (cfg.get('tokens') or {})
            token = (tokens.get(platform) or '').strip()
            if token:
                return token
    except Exception:
        pass
    # 环境变量回退（按平台）
    env_keys_map = {
        'volcengine': ('ARK_API_KEY', 'VOLCENGINE_ARK_API_KEY', 'ARK_TOKEN'),
        'siliconflow': ('SILICONFLOW_API_KEY', 'ARK_API_KEY'),
        'aliyun': ('DASHSCOPE_API_KEY', 'ALIYUN_API_KEY', 'ARK_API_KEY'),
    }
    for env_key in env_keys_map.get(platform, ('ARK_API_KEY', 'VOLCENGINE_ARK_API_KEY', 'ARK_TOKEN')):
        val = (os.environ.get(env_key) or '').strip()
        if val:
            return val
    return ""


def extract_keywords_with_llm(title: str, used_keywords: set = None, keyword_specificity: str = "specific") -> List[str]:
    """使用选定平台/模型从新闻标题中提取最多三个中文关键词。
    
    Args:
        title: 新闻标题
        used_keywords: 已使用的关键词集合，用于避免重复
        keyword_specificity: 关键词特殊性控制 ("broad"=宽泛, "specific"=具体, "balanced"=平衡)
    """
    settings = resolve_llm_settings()
    platform = settings['platform']
    # 针对阿里云增加更稳健的模型回退序列
    primary_model_id = settings['model_id']
    fallback_models: List[str] = []
    if platform == 'aliyun':
        # 常用且更稳定的别名优先，然后回退到可能开放的具体型号
        fallback_models = [
            primary_model_id,
            'qwen-plus',
            'qwen-max',
            'qwen2.5-32b-instruct',
            'qwen2.5-72b-instruct',
            'qwen-turbo',
        ]
    else:
        fallback_models = [primary_model_id]
    token = settings['token']
    base_url = settings['base_url']
    # 打印当前平台与首选模型（用于诊断）
    print(f"正在使用大模型提取关键词（平台: {platform}，模型: {primary_model_id}），标题: {title}")
    if not token:
        print("未找到平台API Token（配置文件或环境变量），跳过关键词提取。")
        return []
    
    if used_keywords is None:
        used_keywords = set()
    
    client = OpenAI(base_url=base_url, api_key=token)
    
    # 构建排除已使用关键词的提示
    exclude_hint = ""
    if used_keywords:
        exclude_list = list(used_keywords)[:10]  # 限制长度避免提示过长
        exclude_hint = f"\n请避免使用这些已经使用过的关键词: {', '.join(exclude_list)}"
    
    # 根据特殊性参数调整提示词
    if keyword_specificity == "broad":
        specificity_instruction = (
            "请提取宽泛的、通用的关键词，能够覆盖更广泛的相关内容。"
            "例如：优先'科技'而非'人工智能'，优先'经济'而非'房价调控'，优先'健康'而非'食品安全'。"
            "关键词应该具有较强的通用性和包容性。"
        )
    elif keyword_specificity == "specific":
        specificity_instruction = (
            "请提取具体的、有特色的关键词，具有较强的区分度和针对性。"
            "例如：优先'人工智能'而非'科技'，优先'房价调控'而非'经济'，优先'食品安全'而非'健康'。"
            "关键词应该能够准确描述新闻的具体主题和内容特点。"
        )
    else:  # balanced
        specificity_instruction = (
            "请提取适中的关键词，既不过于宽泛也不过于具体，保持合适的覆盖面。"
            "在通用性和特异性之间找到平衡，确保关键词既有一定的覆盖面又有足够的区分度。"
        )
    
    prompt = (
        f"请从以下新闻标题中提取最多五个中文关键词，用逗号分隔。"
        f"{specificity_instruction}"
        f"{exclude_hint}"
        "仅返回关键词，不要附加说明。\n\n"
        f"标题: {title}"
    )
    last_err: Optional[Exception] = None
    for model_id in fallback_models:
        try:
            resp = client.chat.completions.create(
                model=model_id,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=96,
            )
            text = (resp.choices[0].message.content or '').strip()
            if not text:
                continue
            raw = [seg.strip() for seg in text.split(',')]
            kws: List[str] = []
            for k in raw:
                k = k.strip()
                if k and k not in kws and k not in used_keywords:
                    kws.append(k)
                if len(kws) >= 3:  # 限制为最多3个关键词
                    break
            if kws:
                return kws
        except Exception as e:
            last_err = e
            # 记录失败但继续回退下一模型
            print(f"模型 {model_id} 提取关键词失败，尝试回退：{e}")
            continue
    if last_err is not None:
        print(f"提取关键词时发生错误（所有回退均失败）: {last_err}")
    return []


def extract_keywords_batch_with_diversity(news_list: List[Dict], keyword_specificity: str = "specific") -> Dict[int, List[str]]:
    """批量提取关键词，确保全局多样性和去重。
    
    Args:
        news_list: 新闻列表
        keyword_specificity: 关键词特殊性控制 ("broad"=宽泛, "specific"=具体, "balanced"=平衡)
        
    Returns:
        Dict[int, List[str]]: 索引到关键词列表的映射
    """
    all_keywords: Dict[int, List[str]] = {}
    used_keywords: set = set()
    
    # 按新闻重要性排序，优先为重要新闻分配关键词
    indexed_news = [(i, item) for i, item in enumerate(news_list)]
    # 如果新闻有评分，按评分排序；否则保持原顺序
    try:
        indexed_news.sort(key=lambda x: x[1].get('score', 50), reverse=True)
    except:
        pass
    
    total_items = len(indexed_news)
    for idx, (i, item) in enumerate(indexed_news):
        title = item.get('title') or ''
        print(f'提取关键词（#{i+1}）：{title}')
        try:
            kws = extract_keywords_with_llm(title, used_keywords, keyword_specificity) or []
            
            # 进一步过滤，确保关键词质量
            filtered_kws: List[str] = []
            for k in kws:
                k = k.strip()
                if (k and len(k) >= 2 and k not in used_keywords and 
                    k not in filtered_kws and len(filtered_kws) < 3):
                    filtered_kws.append(k)
                    used_keywords.add(k)
            
            all_keywords[i] = filtered_kws
            print(f'  -> 关键词：{filtered_kws}')
            
            # 如果某篇新闻没有获得关键词，尝试使用更宽泛的词汇作为备选
            if not filtered_kws and len(used_keywords) < 20:  # 避免过度回退
                fallback_kws = extract_fallback_keywords(title, used_keywords)
                if fallback_kws:
                    all_keywords[i] = fallback_kws[:1]  # 只取一个备选关键词
                    used_keywords.update(fallback_kws[:1])
                    print(f'  -> 备选关键词：{fallback_kws[:1]}')
                    
        except Exception as e:
            print(f'提取关键词失败：{e}')
            all_keywords[i] = []
            
        # 关键词提取阶段性进度（70-85区间线性推进）
        try:
            kw_progress = 70 + int(15 * (idx + 1) / total_items)
            print(f'PROGRESS:{kw_progress}')
        except Exception:
            pass
    
    return all_keywords


def extract_fallback_keywords(title: str, used_keywords: set) -> List[str]:
    """当主要关键词提取失败时，提供备选关键词。"""
    # 基于标题内容的简单规则匹配
    fallback_map = {
        '政策|法规|条例|规定|通知': ['政策法规'],
        '教育|学校|学生|考试|招生': ['教育培训'],
        '医疗|医院|健康|疾病|药品': ['医疗健康'],
        '房价|住房|地产|物业': ['房地产'],
        '交通|出行|道路|车辆': ['交通出行'],
        '环境|污染|生态|绿化': ['环境保护'],
        '科技|技术|创新|研发': ['科技创新'],
        '经济|金融|投资|市场': ['经济金融'],
        '安全|事故|灾害|应急': ['公共安全'],
        '消费|价格|物价|商品': ['消费市场']
    }
    
    title_lower = title.lower()
    candidates = []
    
    for pattern, keywords in fallback_map.items():
        if re.search(pattern, title):
            for kw in keywords:
                if kw not in used_keywords:
                    candidates.append(kw)
    
    return candidates[:2]  # 最多返回2个备选关键词


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / 'output'


def _parse_date_dir_name(name: str) -> Optional[datetime]:
    """尝试将目录名解析为日期对象（格式：YYYY年MM月DD日）。"""
    try:
        return datetime.strptime(name, '%Y年%m月%d日')
    except Exception:
        return None


def find_latest_html_in_deploy() -> Optional[Path]:
    """在部署版 output 下定位最新的 HTML 文件，优先使用当日汇总。"""
    if not OUTPUT_DIR.exists():
        return None

    # 选择日期目录（按名称解析为日期后排序）
    dated_dirs = []
    for d in OUTPUT_DIR.iterdir():
        if d.is_dir():
            dt = _parse_date_dir_name(d.name)
            if dt is not None:
                dated_dirs.append((dt, d))
    if not dated_dirs:
        return None

    # 最新日期目录
    latest_dir = sorted(dated_dirs, key=lambda x: x[0], reverse=True)[0][1]
    html_dir = latest_dir / 'html'
    if not html_dir.exists():
        return None

    # 优先当日汇总
    summary = html_dir / '当日汇总.html'
    if summary.exists():
        return summary

    # 回退：取最新修改时间的 html 文件
    candidates = [f for f in html_dir.iterdir() if f.is_file() and f.suffix.lower() == '.html']
    if not candidates:
        return None
    return sorted(candidates, key=lambda f: f.stat().st_mtime, reverse=True)[0]


def ensure_today_summary() -> Optional[Path]:
    """若缺少当日汇总，仅尝试定位，不再调用其他脚本（保持独立）。"""
    return find_latest_html_in_deploy()


# ---- 内置：配置读取与通用HTML解析/筛选逻辑 ----

def load_pipeline_config() -> Dict:
    """读取 config/config.yaml 中的筛选偏好与限制，若不存在则提供默认。"""
    default_conf = {
        'system_prompt': '你是一个善于判断信息重要性的助手，擅长从大量新闻中挑选最值得关注的要闻。',
        'user_keywords': ['政策', '监管', '产业', '科技', '宏观', '安全', '教育', '健康', '环境', '消费'],
        'exclude_keywords': ['娱乐', '八卦', '广告', '促销', '体育比赛'],
        'limit': 5,
        # 相似度阈值（0-1），用于新闻去重；与前端字段保持一致
        'similarity_threshold': 0.7,
        # 新增：图片抓取的域名白/黑名单（默认尽量选择开源/自由可用来源）
        # 留空表示不启用白名单，仅使用黑名单排除（避免结果为0）
        'image_allowed_domains': [],
        'image_blocked_domains': [
            # 常见的版权不友好或跨站预览易受限的站点（可按需调整）
            'hexun.com', 'cngoldres.com', 'scol.com.cn', 'cn-healthcare.com',
            'csdnimg.cn', 'blog.csdn.net', 'cnblogs.com', 'wezhan.cn',
            'qpic.cn', 'puui.qpic.cn', '51miz.com', 'nipic.cn', '699pic.com',
            'img.xinmin.cn', 'xunmiwang.cn', 'pic.nximg.cn', '0.rc.xiniu.com',
            'img.51wendang.com', 'cdnwww.gaossi.com', 'pic.southmoney.com',
            'img.chyxx.com', 'pic.ibaotu.com', 'yangwei.cn', 'stopnote.vhostgo.com',
            'oss.linstitute.net', 'img.tukuppt.com', 'img3.qianzhan.com', 'p5.itc.cn',
            'zhimg.com', 'img02.tuke88.com', 'bpic.588ku.com', 'voc.com.cn',
            'zykp.daishumed.com', 'www.gov.cn', 'image.yunyingpai.com', 'pic.616pic.com',
            'ylbzj.ahsz.gov.cn', 'fjwsjk.fjsen.com', 'www.cnyxyx.com', 'cms.pixso.cn',
            'd1.faiusr.com', 'www.changbiyuan.com', 'p1-tt.bytecdn.cn', 'www.yebaike.com',
            'static.yueya.net', 'www.1mpi.com', 'mz.eastday.com', 'mz.eastday.com', 'img.shetu66.com',
            'www.8bb.com'
        ],
    }
    try:
        cfg_path = BASE_DIR / 'config' / 'config.yaml'
        if cfg_path.exists():
            import yaml
            with open(cfg_path, 'r', encoding='utf-8') as f:
                raw = yaml.safe_load(f) or {}
            # 允许从顶层或嵌套结构读取
            default_conf['system_prompt'] = (raw.get('system_prompt') or default_conf['system_prompt'])
            default_conf['user_keywords'] = raw.get('user_keywords') or default_conf['user_keywords']
            default_conf['exclude_keywords'] = raw.get('exclude_keywords') or default_conf['exclude_keywords']
            # 允许配置图片域名白/黑名单
            if raw.get('image_allowed_domains') is not None:
                v = raw.get('image_allowed_domains')
                if isinstance(v, str):
                    default_conf['image_allowed_domains'] = [x.strip() for x in v.split(',') if x.strip()]
                elif isinstance(v, list):
                    default_conf['image_allowed_domains'] = v
            if raw.get('image_blocked_domains') is not None:
                v = raw.get('image_blocked_domains')
                if isinstance(v, str):
                    default_conf['image_blocked_domains'] = [x.strip() for x in v.split(',') if x.strip()]
                elif isinstance(v, list):
                    default_conf['image_blocked_domains'] = v
            # 允许从数据库合并的配置结构（兼容web_app逻辑）
            if isinstance(default_conf['user_keywords'], str):
                default_conf['user_keywords'] = [x.strip() for x in default_conf['user_keywords'].split(',') if x.strip()]
            if isinstance(default_conf['exclude_keywords'], str):
                default_conf['exclude_keywords'] = [x.strip() for x in default_conf['exclude_keywords'].split(',') if x.strip()]
            limit_val = raw.get('limit')
            try:
                if limit_val is not None:
                    default_conf['limit'] = max(1, int(limit_val))
            except Exception:
                pass
            # YAML 中的相似度阈值（保持 0-1 范围）
            sim_val = raw.get('similarity_threshold')
            try:
                if sim_val is not None:
                    threshold_val = float(sim_val)
                    if 0.0 <= threshold_val <= 1.0:
                        default_conf['similarity_threshold'] = threshold_val
            except Exception:
                pass
    except Exception:
        pass
    # 优先合并数据库保存的配置（system_prompt、keywords、sources、limit），若存在则覆盖
    try:
        from db import load_app_config  # 部署目录下的SQLite配置
        db_conf = load_app_config() or {}
        # 系统提示词
        sp = db_conf.get('system_prompt')
        if isinstance(sp, str) and sp.strip():
            default_conf['system_prompt'] = sp
        # 用户关键词、排除关键词、图片域名白/黑名单
        for k in ('user_keywords', 'exclude_keywords', 'image_allowed_domains', 'image_blocked_domains'):
            v = db_conf.get(k)
            if isinstance(v, list) and len(v) > 0:
                default_conf[k] = v
            elif isinstance(v, str) and v.strip():
                default_conf[k] = [x.strip() for x in v.split(',') if x.strip()]
        # 数量限制
        lv = db_conf.get('limit')
        try:
            if lv is not None:
                default_conf['limit'] = max(1, int(lv))
        except Exception:
            pass
        # 关键词特殊性控制
        ks = db_conf.get('keyword_specificity')
        if isinstance(ks, str) and ks.strip() and ks in ['broad', 'balanced', 'specific']:
            default_conf['keyword_specificity'] = ks
        # 新闻相似度阈值
        st = db_conf.get('similarity_threshold')
        try:
            if st is not None:
                threshold_val = float(st)
                if 0.0 <= threshold_val <= 1.0:
                    # 主键：similarity_threshold（与前端一致）
                    default_conf['similarity_threshold'] = threshold_val
                    # 兼容旧代码：同时提供别名，避免引用旧键时报错
                    default_conf['news_similarity_threshold'] = threshold_val
        except Exception:
            pass
    except Exception:
        # 忽略数据库不可用的情况，继续使用文件与环境变量配置
        pass
    # 环境变量覆盖 limit（供 Web 前端按钮控制输出数量）
    try:
        env_limit = os.environ.get('AI_FILTER_LIMIT')
        if env_limit is not None:
            default_conf['limit'] = max(1, int(env_limit))
    except Exception:
        pass
    return default_conf


def extract_source_from_url(url: Optional[str]) -> str:
    """根据URL推断来源（域名）。"""
    if not url:
        return '未知来源'
    try:
        from urllib.parse import urlparse
        netloc = urlparse(url).netloc
        return netloc or '未知来源'
    except Exception:
        return '未知来源'


def parse_summary_html(html_path: Path) -> List[Dict[str, Optional[str]]]:
    """通用解析：从当日汇总/HTML页面中抽取新闻条目（标题、链接、来源）。"""
    items: List[Dict[str, Optional[str]]] = []
    if not html_path.exists():
        return items
    # 读取内容，优先UTF-8，回退GBK
    try:
        with open(html_path, 'r', encoding='utf-8') as f:
            html = f.read()
    except UnicodeDecodeError:
        with open(html_path, 'r', encoding='gbk') as f:
            html = f.read()

    soup = BeautifulSoup(html, 'html.parser')

    # 策略1：常规列表/卡片结构中的链接
    for a in soup.find_all('a'):
        try:
            href = (a.get('href') or '').strip()
            title = (a.get_text(strip=True) or '').strip()
            if not href or not title:
                continue
            if href.startswith('javascript:'):
                continue
            if not href.startswith('http'):
                # 相对路径通常不是新闻外链，忽略
                continue
            # 过滤噪声：标题过短或为“打开原图”等
            if len(title) < 6:
                continue
            if any(x in title for x in ['打开原图', '预览', '查看']):
                continue
            source = extract_source_from_url(href)
            items.append({'title': title, 'url': href, 'source': source})
        except Exception:
            continue

    # 去重（按标题规范化）
    def norm(t: str) -> str:
        t = t.lower()
        t = re.sub(r"[\s\-_,.:;，。！？!?:;\[\]\(\)\{\}·•]+", "", t)
        return t
    seen = set()
    uniq: List[Dict[str, Optional[str]]] = []
    for it in items:
        ti = it.get('title') or ''
        nt = norm(ti)
        if not nt:
            continue
        if nt in seen:
            continue
        seen.add(nt)
        uniq.append(it)
    return uniq


def calculate_text_similarity(text1: str, text2: str) -> float:
    """计算两个文本的相似度（0-1之间）"""
    if not text1 or not text2:
        return 0.0
    return SequenceMatcher(None, text1.lower(), text2.lower()).ratio()


def extract_key_terms(title: str) -> set:
    """从标题中提取关键词汇，用于相似度检测"""
    # 移除标点符号和数字，提取主要词汇
    import re
    # 保留中文、英文字母
    cleaned = re.sub(r'[^\u4e00-\u9fa5a-zA-Z\s]', ' ', title)
    # 分割成词汇（简单按空格和长度分割）
    words = set()
    for word in cleaned.split():
        word = word.strip()
        if len(word) >= 2:  # 至少2个字符
            words.add(word.lower())
    
    # 对于中文，尝试提取2-4字的词组
    chinese_chars = re.findall(r'[\u4e00-\u9fa5]+', title)
    for chars in chinese_chars:
        if len(chars) >= 4:
            # 提取2-4字的子串作为关键词
            for i in range(len(chars) - 1):
                for length in [2, 3, 4]:
                    if i + length <= len(chars):
                        words.add(chars[i:i+length])
    
    return words


def check_news_similarity(news1: Dict, news2: Dict, similarity_threshold: float = 0.7) -> bool:
    """检查两条新闻是否相似"""
    title1 = news1.get('title', '') or ''
    title2 = news2.get('title', '') or ''
    
    # 方法1: 直接文本相似度
    text_sim = calculate_text_similarity(title1, title2)
    if text_sim >= similarity_threshold:
        return True
    
    # 方法2: 关键词重叠度
    terms1 = extract_key_terms(title1)
    terms2 = extract_key_terms(title2)
    
    if not terms1 or not terms2:
        return False
    
    # 计算Jaccard相似度
    intersection = len(terms1 & terms2)
    union = len(terms1 | terms2)
    jaccard_sim = intersection / union if union > 0 else 0
    
    # 如果关键词重叠度超过阈值，认为相似
    return jaccard_sim >= 0.4  # 40%的关键词重叠


def deduplicate_similar_news(news_list: List[Dict], similarity_threshold: float = 0.7) -> List[Dict]:
    """去除相似的新闻，保留评分更高的"""
    if len(news_list) <= 1:
        return news_list
    
    # 按评分降序排序，优先保留高分新闻
    sorted_news = sorted(news_list, key=lambda x: x.get('score', 0), reverse=True)
    
    result = []
    for current in sorted_news:
        is_similar = False
        for existing in result:
            if check_news_similarity(current, existing, similarity_threshold):
                is_similar = True
                print(f"发现相似新闻，跳过：{current.get('title', '')[:30]}...")
                break
        
        if not is_similar:
            result.append(current)
    
    return result


def ai_select_news(news: List[Dict[str, Optional[str]]], limit: int, conf: Dict, similarity_threshold: Optional[float] = None) -> List[Dict[str, Optional[str]]]:
    """使用选定平台/模型对所有候选逐条打分，按分数降序取前N并返回（附带理由）。
    若无Token或模型失败，则使用规则打分并排序。
    
    Args:
        news: 候选新闻列表
        limit: 需要选择的新闻数量
        conf: 配置字典
        similarity_threshold: 新闻相似度阈值，用于去重；None 时从 conf 获取
    """
    settings = resolve_llm_settings()
    token = settings['token']
    base_url = settings['base_url']
    model_id = settings['model_id']
    platform = settings['platform']

    def rule_score_all(items: List[Dict[str, Optional[str]]]) -> None:
        keys = [k.lower() for k in (conf.get('user_keywords') or []) if isinstance(k, str)]
        excl = [e.lower() for e in (conf.get('exclude_keywords') or []) if isinstance(e, str)]
        for it in items:
            title = (it.get('title') or '').lower()
            hits = [k for k in keys if k and k in title]
            bads = [e for e in excl if e and e in title]
            s = 50
            s += 10 * len(hits)
            s -= 15 * len(bads)
            s = max(0, min(100, s))
            it['score'] = s
            pieces: List[str] = []
            if hits:
                pieces.append(f"命中关键词:{', '.join(hits)}")
            if bads:
                pieces.append(f"含排除项:{', '.join(bads)}")
            if not pieces:
                pieces.append("主题相关度一般")
            reason = "；".join(pieces)
            it['reason'] = reason[:48]

    if token:
        try:
            client = OpenAI(base_url=base_url, api_key=token)
            user_keywords = conf.get('user_keywords') or []
            exclude_keywords = conf.get('exclude_keywords') or []
            system_prompt = conf.get('system_prompt') or ''
            print(f"使用LLM模型进行打分：{model_id}（平台: {platform}），并按分排序取前{limit}条")
            # 分批评分，避免上下文过长（调小批次以降低截断与解析失败概率）
            batch_size = 5
            total = len(news)
            for start in range(0, total, batch_size):
                end = min(start + batch_size, total)
                lines = []
                for i in range(start, end):
                    it = news[i]
                    src = it.get('source') or '未知来源'
                    title = it.get('title') or ''
                    url = it.get('url') or ''
                    line = f"{i+1}. [{src}] {title}"
                    if url:
                        line += f"\nURL: {url}"
                    lines.append(line)
                prompt = (
                    f"{system_prompt}\n\n"
                    "你是新闻筛选与评分助手。请对下列候选逐条评分并给出简短理由。\n"
                    "评分规则：只依据关键词与排除项；命中关键词加分，命中排除项减分。\n"
                    "请严格依据提供的关键词列表进行判断，不要主观拓展语义。\n"
                    "只返回严格的 JSON 数组（不得包含额外文字或代码块），顺序与输入一致。\n"
                    "每个对象必须包含：id（整数，原序号）、score（整数0-100）、reason（不超过24字的中文短句）。\n\n"
                    "偏好关键词: " + ", ".join(user_keywords) + "\n"
                    "排除关键词: " + ", ".join(exclude_keywords) + "\n"
                    "候选列表:\n" + "\n".join(lines)
                )
                resp = client.chat.completions.create(
                    model=model_id,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=1536,
                )
                text = (resp.choices[0].message.content or '').strip()
                # 兼容模型输出包含代码块或前后说明文字的情况
                # 优先提取 ```json ... ``` 内的内容，其次提取首个数组片段
                fenced = False
                m_code = re.search(r"```json\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
                if m_code:
                    text_candidate = m_code.group(1).strip()
                    fenced = True
                else:
                    m_arr = re.search(r"\[\s*\{[\s\S]*\}\s*\]", text)
                    text_candidate = m_arr.group(0).strip() if m_arr else ""
                if not text_candidate:
                    print("当前批次评分解析失败，改用规则评分该批次。")
                    rule_score_all(news[start:end])
                    continue
                import json as _json
                try:
                    arr = _json.loads(text_candidate)
                    if not isinstance(arr, list):
                        print("当前批次评分返回非数组，改用规则评分该批次。")
                        rule_score_all(news[start:end])
                        continue
                    # 放宽长度匹配要求：允许返回少于批次的条目，按 id 对应更新
                    for j, obj in enumerate(arr):
                        try:
                            raw_id = obj.get('id')
                            if raw_id is not None:
                                idx = int(raw_id) - 1
                            else:
                                # 无 id 时按顺序映射
                                idx = start + j
                            if idx < start or idx >= end:
                                continue
                            score = int(obj.get('score'))
                            reason = obj.get('reason')
                            it = news[idx]
                            it['score'] = max(0, min(100, score))
                            if reason:
                                it['reason'] = str(reason)[:48]
                        except Exception:
                            continue
                except Exception:
                    print("当前批次评分JSON解析异常，改用规则评分该批次。")
                    rule_score_all(news[start:end])
        except Exception as e:
            print(f"LLM批量打分失败，改用规则评分：{e}")
            rule_score_all(news)
    else:
        # 无Token：规则评分
        rule_score_all(news)

    # 根据分数排序并取前N
    with_scores = [it for it in news if isinstance(it.get('score'), (int, float))]
    with_scores.sort(key=lambda x: x.get('score', 0), reverse=True)
    
    # 获取相似度阈值配置（优先使用函数参数，其次使用配置文件，最后默认 0.7）
    final_similarity_threshold = similarity_threshold if similarity_threshold is not None else conf.get('similarity_threshold', conf.get('news_similarity_threshold', 0.7))
    
    # 先选择更多候选，然后去重
    candidates = with_scores[:limit * 2]  # 选择2倍数量作为候选
    
    # 去除相似新闻
    deduplicated = deduplicate_similar_news(candidates, final_similarity_threshold)
    
    # 如果去重后数量不足，补充更多候选
    if len(deduplicated) < limit and len(with_scores) > len(candidates):
        additional_candidates = with_scores[len(candidates):limit * 3]
        all_candidates = deduplicated + additional_candidates
        deduplicated = deduplicate_similar_news(all_candidates, final_similarity_threshold)
    
    return deduplicated[:limit]


def quick_rule_score(items: List[Dict[str, Optional[str]]], conf: Dict) -> None:
    """快速规则打分，用于构建LLM评分的候选池。"""
    keys = [k.lower() for k in (conf.get('user_keywords') or []) if isinstance(k, str)]
    excl = [e.lower() for e in (conf.get('exclude_keywords') or []) if isinstance(e, str)]
    for it in items:
        title = (it.get('title') or '').lower()
        hits = [k for k in keys if k and k in title]
        bads = [e for e in excl if e and e in title]
        s = 50 + 10 * len(hits) - 15 * len(bads)
        it['score'] = max(0, min(100, s))


def score_news_items(items: List[Dict[str, Optional[str]]], conf: Dict) -> List[Dict[str, Optional[str]]]:
    """为已选新闻生成评分与一句话理由（保留以兼容旧流程）。
    当前流程已在选择阶段生成分数与理由，通常无需再次调用。"""
    settings = resolve_llm_settings()
    token = settings['token']
    base_url = settings['base_url']
    model_id = settings['model_id']
    platform = settings['platform']
    user_keywords = conf.get('user_keywords') or []
    exclude_keywords = conf.get('exclude_keywords') or []
    system_prompt = conf.get('system_prompt') or ''

    if token and items:
        try:
            client = OpenAI(base_url=base_url, api_key=token)
            lines = []
            for i, it in enumerate(items, 1):
                src = it.get('source') or '未知来源'
                title = it.get('title') or ''
                url = it.get('url') or ''
                line = f"{i}. [{src}] {title}"
                if url:
                    line += f"\nURL: {url}"
                lines.append(line)
            prompt = (
                f"{system_prompt}\n\n"
                "你是一位政策新闻筛选助手。已选出若干候选，请为每条候选给出相关性评分并简要理由。\n"
                "评分依据包括：主题是否匹配偏好关键词、是否命中排除项。\n"
                "返回严格的 JSON 数组，长度与输入一致且顺序对应。\n"
                "每个对象包含：score（整数0-100）、reason（不超过24字的中文短句）。\n"
                "不要返回除数组外的任何文本。\n\n"
                "偏好关键词: " + ", ".join(user_keywords) + "\n"
                "排除关键词: " + ", ".join(exclude_keywords) + "\n\n"
                "候选列表:\n" + "\n".join(lines)
            )
            print(f"开始为已选新闻生成模型评分与理由（平台: {platform}，模型: {model_id}）")
            resp = client.chat.completions.create(
                model=model_id,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=512,
            )
            text = (resp.choices[0].message.content or '').strip()
            import json as _json
            import re as _re
            m = _re.search(r"\[\s*\{[\s\S]*\}\s*\]", text)
            if m:
                arr_text = m.group(0)
                try:
                    data = _json.loads(arr_text)
                    if isinstance(data, list) and len(data) == len(items):
                        for it, obj in zip(items, data):
                            try:
                                score = int(obj.get('score'))
                            except Exception:
                                score = None
                            reason = obj.get('reason')
                            if score is not None:
                                it['score'] = score
                            if reason:
                                it['reason'] = str(reason)
                        return items
                except Exception:
                    pass
            print("模型评分结果解析失败，改用规则回退评分。")
        except Exception as e:
            print(f"模型评分失败，改用规则回退评分：{e}")

    # 规则回退：基于关键词命中、排除项生成分数与理由（不再考虑来源偏好）
    keys = [k.lower() for k in (conf.get('user_keywords') or []) if isinstance(k, str)]
    excl = [e.lower() for e in (conf.get('exclude_keywords') or []) if isinstance(e, str)]
    for it in items:
        title = (it.get('title') or '').lower()
        hits = [k for k in keys if k and k in title]
        bads = [e for e in excl if e and e in title]
        s = 50  # 基础分
        s += 10 * len(hits)
        s -= 15 * len(bads)
        # 限制范围 0-100
        s = max(0, min(100, s))
        it['score'] = s
        # 生成简短理由
        pieces: List[str] = []
        if hits:
            pieces.append(f"命中关键词:{', '.join(hits)}")
        if bads:
            pieces.append(f"含排除项:{', '.join(bads)}")
        if not pieces:
            pieces.append("主题相关度一般")
        reason = "；".join(pieces)
        # 截断到24字左右（中文计数简化为字符长度）
        it['reason'] = reason[:48]  # 允许略超，防止语义不完整
    return items


def fetch_bing_image_urls(keyword: str, count: int = 10) -> List[str]:
    """从 Bing 图片搜索抓取图片链接，尽量返回直接图片 URL。
    依据配置的域名白/黑名单进行过滤，避免使用版权不友好来源。"""
    # 加载域名白/黑名单
    conf = load_pipeline_config()
    allowed_domains: List[str] = [d.lower() for d in conf.get('image_allowed_domains', []) if isinstance(d, str)]
    blocked_domains: List[str] = [d.lower() for d in conf.get('image_blocked_domains', []) if isinstance(d, str)]

    def domain_of(url: str) -> str:
        try:
            from urllib.parse import urlparse
            return (urlparse(url).netloc or '').lower()
        except Exception:
            return ''

    def is_allowed(url: str) -> bool:
        host = domain_of(url)
        if not host:
            return False
        # 若配置了白名单，仅允许白名单
        if allowed_domains:
            return any(host.endswith(d) or host == d for d in allowed_domains)
        # 否则仅排除黑名单
        if blocked_domains and any(host.endswith(d) or host == d for d in blocked_domains):
            return False
        return True
    urls: List[str] = []
    seen = set()
    q = keyword.strip()
    if not q:
        return urls
    params = {
        'q': q,
        'first': '1',
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Referer': 'https://cn.bing.com/',
    }
    raw_candidates: List[str] = []
    try:
        resp = requests.get('https://cn.bing.com/images/search', params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        html = resp.text
        soup = BeautifulSoup(html, 'html.parser')

        # 解析 a 元素的 m 属性（JSON，包含 murl/imgurl）
        for a in soup.find_all('a', class_='iusc'):
            m_attr = a.get('m')
            if not m_attr:
                continue
            try:
                data = json.loads(m_attr)
                cand = data.get('murl') or data.get('imgurl') or data.get('turl')
                if cand:
                    raw_candidates.append(cand)
                    if cand not in seen and is_allowed(cand):
                        seen.add(cand)
                        urls.append(cand)
                        if len(urls) >= count:
                            break
            except Exception:
                continue

        # 回退：解析 img 标签的 src/data-src
        if len(urls) < count:
            for img in soup.find_all('img'):
                cand = img.get('src') or img.get('data-src') or img.get('data-original')
                if cand and cand.startswith('http'):
                    raw_candidates.append(cand)
                    if cand not in seen and is_allowed(cand):
                        seen.add(cand)
                        urls.append(cand)
                        if len(urls) >= count:
                            break
    except Exception as e:
        print(f'抓取 Bing 图片失败（{keyword}）: {e}')
    # 若启用了白名单但结果不足，放宽到仅使用黑名单（避免全部为0）
    if allowed_domains and len(urls) < count:
        for cand in raw_candidates:
            if cand in urls:
                continue
            host = domain_of(cand)
            if blocked_domains and any(host.endswith(d) or host == d for d in blocked_domains):
                continue
            urls.append(cand)
            if len(urls) >= count:
                break
    return urls[:count]


def build_html(news_list: List[Dict[str, Optional[str]]], all_keywords: Dict[int, List[str]], images: Dict[int, Dict[str, List[str]]], notice: Optional[str] = None) -> str:
    """构建汇总 HTML 页面。"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    head = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>图片抓取结果 - Bing</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, 'Microsoft YaHei', sans-serif; margin: 20px; }}
    .news {{ margin-bottom: 28px; padding: 16px; border: 1px solid #e5e7eb; border-radius: 8px; }}
    .title {{ font-weight: 600; font-size: 16px; color: #111827; }}
    .meta {{ color: #6b7280; font-size: 12px; margin-top: 4px; }}
    .keywords {{ margin-top: 8px; color: #374151; font-size: 13px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 8px; margin-top: 12px; }}
    .card {{ border: 1px solid #e5e7eb; border-radius: 6px; overflow: hidden; background: #fff; }}
    .card img {{ width: 100%; height: 120px; object-fit: cover; display: block; }}
    .card a {{ display: block; text-decoration: none; color: #2563eb; font-size: 12px; padding: 6px; }}
    .footer {{ margin-top: 24px; color: #6b7280; font-size: 12px; text-align: center; }}
    .notice {{ margin-top: 12px; padding: 12px; border-radius: 6px; background: #FFF4E5; border: 1px solid #FBD38D; color: #8A6D3B; font-size: 13px; }}
  </style>
</head>
<body>
  <h2>图片抓取结果（Bing）</h2>
  <div class="meta">生成时间：{now}</div>
"""

    body = []
    for idx, item in enumerate(news_list, 1):
        title = item.get('title') or '无标题'
        source = item.get('source') or '未知来源'
        url = item.get('url')
        kw_list = all_keywords.get(idx - 1, [])
        title_html = title
        if url:
            title_html = f'<a href="{url}" target="_blank" rel="noopener noreferrer">{title}</a>'
        body.append('<div class="news">')
        body.append(f'<div class="title">{idx}. {title_html}</div>')
        body.append(f'<div class="meta">来源：{source}</div>')
        score = item.get('score')
        reason = item.get('reason')
        if score is not None:
            body.append(f'<div class="meta">评分：{score}/100</div>')
        if reason:
            body.append(f'<div class="meta">理由：{reason}</div>')
        if kw_list:
            body.append(f'<div class="keywords">关键词：{", ".join(kw_list)}</div>')
        # 图片网格
        grid = []
        news_images = images.get(idx - 1, {})
        for kw in kw_list:
            urls = news_images.get(kw, [])
            if not urls:
                continue
            grid.append(f'<div class="meta" style="margin-top:10px;">关键词「{kw}」的图片（{len(urls)}）</div>')
            grid.append('<div class="grid">')
            for u in urls:
                # 加强跨站预览兼容性；并展示原图链接
                grid.append(
                    f'<div class="card">'
                    f'<img src="{u}" alt="{kw}" referrerpolicy="no-referrer" loading="lazy" />'
                    f'<a href="{u}" target="_blank" rel="noopener noreferrer">打开原图</a>'
                    f'</div>'
                )
            grid.append('</div>')
        body.extend(grid)
        body.append('</div>')

    tail = """
  <div class="footer">TrendRadar · Bing 图片抓取结果</div>
</body>
</html>
"""
    # 提示栏（当可用结果不足用户请求的数量时显示）
    notice_html = f"\n<div class=\"notice\">{notice}</div>\n" if notice else ""
    return head + notice_html + "\n".join(body) + tail


def run_pipeline(keyword_specificity: str = "specific", news_similarity_threshold: float = 0.7) -> Optional[Path]:
    """运行筛选与图片抓取整合流程（完全独立，不依赖其他代码文件）。返回输出文件路径。
    
    Args:
        keyword_specificity: 关键词特殊性控制 ("broad"=宽泛, "specific"=具体, "balanced"=平衡)
        news_similarity_threshold: 新闻相似度阈值，用于去重相似新闻
    """
    # 线性进度：起始
    print('PROGRESS:5')
    # 1) 定位输入HTML（优先当日汇总），不调用其他脚本
    html_path = ensure_today_summary()
    if html_path is None:
        print('错误：未找到当日汇总或任何HTML文件，无法继续。请先生成当日汇总页面。')
        return None
    # 详细日志：读取文件
    try:
        print(f"读取文件：{html_path}")
    except Exception:
        print("读取文件：<未知路径>")

    # 新逻辑：若使用当日汇总，则清理时间点HTML文件，并同步清理对应的TXT文件
    try:
        if html_path.name == '当日汇总.html':
            pass
        else:
            print(f"当前使用时间点文件：{html_path.name}")
    except Exception as e:
        print(f"清理时间点文件时发生异常：{e}")
    print('PROGRESS:10')

    # 2) 解析候选新闻并去重
    candidates = parse_summary_html(html_path)
    if not candidates:
        print('错误：未能从输入HTML解析到任何新闻条目。')
        return None
    print(f'解析候选新闻：{len(candidates)} 条')
    print('PROGRESS:20')

    # 3) 读取筛选配置，并使用LLM或关键词匹配选择Top-N
    conf = load_pipeline_config()
    # 详细日志：平台与模型、提示词与关键词
    try:
        settings = resolve_llm_settings()
        print(f"使用平台 {settings['platform']} 模型 {settings['model_id']}")
        print(f"提示词：{conf.get('system_prompt') or ''}")
        print(f"用户关键词：{', '.join(conf.get('user_keywords') or [])}")
        print(f"排除关键词：{', '.join(conf.get('exclude_keywords') or [])}")
    except Exception:
        pass
    print('PROGRESS:30')
    limit = conf.get('limit', 5)
    print(f"开始筛选，目标数量：{limit}")
    print('PROGRESS:40')
    # 先构建LLM评分候选池（规则快速打分，选前max(60,limit*12)）
    pool_size = min(len(candidates), max(60, limit * 12))
    # 严格关键词预过滤：必须命中至少一个用户关键词，且不得命中排除关键词
    keys = [k.lower() for k in (conf.get('user_keywords') or []) if isinstance(k, str) and k.strip()]
    excl = [e.lower() for e in (conf.get('exclude_keywords') or []) if isinstance(e, str) and e.strip()]
    cand_pool = []
    for it in candidates:
        title_lower = (it.get('title') or '').lower()
        if keys and not any(k in title_lower for k in keys):
            continue
        if any(e in title_lower for e in excl):
            continue
        cand_pool.append(dict(it))
    # 若过滤后候选为空，则回退为全部候选以避免流程中断
    if not cand_pool:
        cand_pool = [dict(it) for it in candidates]
    quick_rule_score(cand_pool, conf)
    cand_pool.sort(key=lambda it: it.get('score', 0), reverse=True)
    cand_pool = cand_pool[:pool_size]
    print(f"预筛选：规则打分选出前{pool_size}条进入LLM评分池")
    # 统一使用配置中的相似度阈值（兼容旧键），如未配置则回退为函数参数默认值
    sim_threshold = conf.get('similarity_threshold', conf.get('news_similarity_threshold', news_similarity_threshold))
    try:
        print(f"相似度阈值：{sim_threshold}")
    except Exception:
        pass
    selected = ai_select_news(cand_pool, limit=limit, conf=conf, similarity_threshold=sim_threshold)
    if not selected:
        print('错误：筛选流程未得到任何新闻。')
        return None
    print(f'整合流程：评分排序已选 {len(selected)} 条新闻')
    # 已在选择阶段生成分数与理由，仅打印日志
    for i, it in enumerate(selected, 1):
        title = it.get('title') or ''
        score = it.get('score')
        reason = it.get('reason') or ''
        if score is not None:
            print(f"评分（#{i}）：{title} -> {score}/100；理由：{reason}")
    print('PROGRESS:60')

    # 4) 转换为用于生成页面的结构
    news_list: List[Dict[str, Optional[str]]] = []
    for item in selected:
        news_list.append({
            'title': item.get('title'),
            'source': item.get('source'),
            'url': item.get('url'),
            'score': item.get('score'),
            'reason': item.get('reason'),
        })
    print('PROGRESS:65')

    # 在筛选结果不足时生成提示
    notice_msg: Optional[str] = None
    try:
        if len(news_list) < limit:
            notice_msg = (
                f"提示：根据匹配规则只能获取到 {len(news_list)} 条新闻，"
                f"无法输出你选择的 {limit} 条。系统已返回全部匹配结果。"
            )
            print(notice_msg)
    except Exception:
        pass

    # 5) 关键词提取（统一使用网页端选择的平台与模型）
    kw_settings = resolve_llm_settings()
    if not kw_settings.get('token'):
        try:
            print(f"提示：未从配置/环境读取到 {kw_settings.get('platform','未知平台')} 的 API Token，关键词提取可能为空。请到配置页填写。")
        except Exception:
            print('提示：未读取到平台API Token，关键词提取可能为空。')
    
    # 使用新的批量关键词提取函数，确保关键词多样性
    # 从配置中读取关键词特殊性（优先级高于函数默认），允许值：broad/balanced/specific
    ks = (conf.get('keyword_specificity') or '').strip().lower()
    if ks not in ['broad', 'balanced', 'specific']:
        ks = keyword_specificity
    try:
        print(f'关键词特殊性模式：{ks}')
    except Exception:
        pass
    print('开始批量提取关键词，确保多样性...')
    all_keywords: Dict[int, List[str]] = extract_keywords_batch_with_diversity(news_list, ks)
    print('PROGRESS:85')

    # 6) 抓取 Bing 图片（每个关键词 10 张）
    images: Dict[int, Dict[str, List[str]]] = {}
    # 统计总抓取任务数并线性推进（85-98区间）
    total_fetch_tasks = sum(len(kws) for kws in all_keywords.values())
    completed_fetch = 0
    for i, kws in all_keywords.items():
        images[i] = {}
        for kw in kws:
            # 进度提示仅显示关键词，不显示新闻标题
            print(f'寻找图片：关键词「{kw}」')
            urls = fetch_bing_image_urls(kw, count=10)
            images[i][kw] = urls
            print(f'  -> 获取到 {len(urls)} 张图片')
            time.sleep(0.6)  # 轻微节流
            completed_fetch += 1
            try:
                if total_fetch_tasks > 0:
                    fetch_progress = 85 + int(13 * completed_fetch / total_fetch_tasks)
                    print(f'PROGRESS:{fetch_progress}')
            except Exception:
                pass

    # 7) 生成 HTML 并保存到 output/当天/html（固定文件名，覆盖写入）
    today = datetime.now().strftime('%Y年%m月%d日')
    html_dir = OUTPUT_DIR / today / 'html'
    html_dir.mkdir(parents=True, exist_ok=True)
    outfile = html_dir / '图片抓取结果_Bing.html'
    html_content = build_html(news_list, all_keywords, images, notice=notice_msg)
    # 原子写入：先写入临时文件，完成后再替换正式文件，避免中途停止破坏旧结果
    tmpfile = html_dir / '图片抓取结果_Bing.tmp'
    with open(tmpfile, 'w', encoding='utf-8') as f:
        f.write(html_content)
    try:
        os.replace(tmpfile, outfile)
    except Exception:
        # 若替换失败，保留旧文件与临时文件，便于诊断
        pass
    print(f'图片抓取结果已保存: {outfile}')
    print('PROGRESS:98')

    # 8) 输出可预览链接（供前端 /api/preview 使用）
    rel_path = str(outfile.relative_to(OUTPUT_DIR)).replace('\\', '/')
    preview_url = f'/api/preview/{rel_path}'
    print(f'生成链接：{preview_url}')
    print('PROGRESS:100')
    return outfile


def main():
    run_pipeline()


if __name__ == '__main__':
    main()
