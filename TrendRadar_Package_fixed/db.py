#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import sqlite3
from pathlib import Path
from datetime import datetime

# 使用与模块文件同级的绝对路径，避免受当前工作目录影响
BASE_DIR = Path(__file__).resolve().parent
DB_DIR = BASE_DIR / "data"
DB_PATH = DB_DIR / "app.db"

SCHEMA = {
    "app_config": (
        "CREATE TABLE IF NOT EXISTS app_config ("
        "id INTEGER PRIMARY KEY,"
        "config_json TEXT NOT NULL,"
        "updated_at TEXT NOT NULL"
        ")"
    ),
    "user_tokens": (
        "CREATE TABLE IF NOT EXISTS user_tokens ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "platform TEXT NOT NULL UNIQUE,"
        "token TEXT NOT NULL,"
        "created_at TEXT NOT NULL,"
        "updated_at TEXT NOT NULL"
        ")"
    ),
    "user_prompts": (
        "CREATE TABLE IF NOT EXISTS user_prompts ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "prompt_type TEXT NOT NULL,"  # system_prompt, user_keywords, exclude_keywords, preferred_sources
        "content TEXT NOT NULL,"
        "created_at TEXT NOT NULL,"
        "updated_at TEXT NOT NULL"
        ")"
    ),
    "filter_results": (
        "CREATE TABLE IF NOT EXISTS filter_results ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "task_name TEXT NOT NULL,"
        "platform TEXT,"
        "model_type TEXT,"
        "filter_config TEXT,"  # JSON格式存储筛选配置
        "result_content TEXT,"  # 筛选结果内容
        "result_path TEXT,"     # 结果文件路径
        "news_count INTEGER DEFAULT 0,"  # 筛选出的新闻数量
        "created_at TEXT NOT NULL"
        ")"
    ),
    "run_history": (
        "CREATE TABLE IF NOT EXISTS run_history ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "task_name TEXT NOT NULL,"
        "platform TEXT,"
        "model_type TEXT,"
        "status TEXT,"
        "message TEXT,"
        "result_path TEXT,"
        "created_at TEXT NOT NULL,"
        "completed_at TEXT"
        ")"
    ),
}


def get_connection() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        cur = conn.cursor()
        for _, ddl in SCHEMA.items():
            cur.execute(ddl)
        conn.commit()
    finally:
        conn.close()


def save_app_config(config: dict) -> bool:
    ts = datetime.now().isoformat(timespec="seconds")
    payload = json.dumps(config, ensure_ascii=False)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO app_config (id, config_json, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET config_json=excluded.config_json, updated_at=excluded.updated_at",
            (payload, ts),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


def load_app_config() -> dict | None:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT config_json FROM app_config WHERE id=1")
        row = cur.fetchone()
        if row:
            try:
                return json.loads(row["config_json"])  # type: ignore[index]
            except Exception:
                return None
        return None
    finally:
        conn.close()


# Token管理函数
def save_user_token(platform: str, token: str) -> bool:
    """保存用户Token"""
    ts = datetime.now().isoformat(timespec="seconds")
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO user_tokens (platform, token, created_at, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(platform) DO UPDATE SET token=excluded.token, updated_at=excluded.updated_at",
            (platform, token, ts, ts),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


def load_user_token(platform: str) -> str | None:
    """加载用户Token"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT token FROM user_tokens WHERE platform=?", (platform,))
        row = cur.fetchone()
        return row["token"] if row else None
    finally:
        conn.close()


def load_all_user_tokens() -> dict:
    """加载所有用户Token"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT platform, token FROM user_tokens")
        rows = cur.fetchall()
        return {row["platform"]: row["token"] for row in rows}
    finally:
        conn.close()


# 提示词配置管理函数
def save_user_prompt(prompt_type: str, content: str) -> bool:
    """保存用户提示词配置"""
    ts = datetime.now().isoformat(timespec="seconds")
    conn = get_connection()
    try:
        cur = conn.cursor()
        # 先删除旧的配置
        cur.execute("DELETE FROM user_prompts WHERE prompt_type=?", (prompt_type,))
        # 插入新配置
        cur.execute(
            "INSERT INTO user_prompts (prompt_type, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (prompt_type, content, ts, ts),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


def load_user_prompt(prompt_type: str) -> str | None:
    """加载用户提示词配置"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT content FROM user_prompts WHERE prompt_type=? ORDER BY updated_at DESC LIMIT 1", (prompt_type,))
        row = cur.fetchone()
        return row["content"] if row else None
    finally:
        conn.close()


def load_all_user_prompts() -> dict:
    """加载所有用户提示词配置"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT prompt_type, content FROM user_prompts")
        rows = cur.fetchall()
        return {row["prompt_type"]: row["content"] for row in rows}
    finally:
        conn.close()


# 筛选结果管理函数
def save_filter_result(task_name: str, platform: str | None, model_type: str | None, 
                      filter_config: dict, result_content: str, result_path: str | None, 
                      news_count: int = 0) -> bool:
    """保存筛选结果"""
    ts = datetime.now().isoformat(timespec="seconds")
    filter_config_json = json.dumps(filter_config, ensure_ascii=False)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO filter_results (task_name, platform, model_type, filter_config, result_content, result_path, news_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_name, platform, model_type, filter_config_json, result_content, result_path, news_count, ts),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


def load_recent_filter_results(limit: int = 20) -> list[dict]:
    """加载最近的筛选结果"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, task_name, platform, model_type, filter_config, result_content, result_path, news_count, created_at "
            "FROM filter_results ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
        results = []
        for row in rows:
            result = dict(row)
            try:
                result["filter_config"] = json.loads(result["filter_config"])
            except:
                result["filter_config"] = {}
            results.append(result)
        return results
    finally:
        conn.close()


def add_run_history(task_name: str, platform: str | None, model_type: str | None, status: str, message: str, result_path: str | None, completed: bool = True) -> None:
    conn = get_connection()
    try:
        cur = conn.cursor()
        now = datetime.now().isoformat(timespec="seconds")
        completed_at = now if completed else None
        cur.execute(
            "INSERT INTO run_history (task_name, platform, model_type, status, message, result_path, created_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_name, platform, model_type, status, message, result_path, now, completed_at),
        )
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


def recent_run_history(limit: int = 20) -> list[dict]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, task_name, platform, model_type, status, message, result_path, created_at, completed_at "
            "FROM run_history ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()