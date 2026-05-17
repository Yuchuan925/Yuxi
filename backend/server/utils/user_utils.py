"""
用户ID生成工具
提供用户名验证和user_id自动生成功能
"""

import re

from pypinyin import lazy_pinyin, Style


def to_pinyin(text: str) -> str:
    """
    将中文转换为拼音
    使用pypinyin库进行转换
    """
    # 使用pypinyin进行转换
    pinyin_list = lazy_pinyin(text, style=Style.NORMAL)
    return "".join(pinyin_list)


def validate_username(username: str) -> tuple[bool, str]:
    """
    验证用户名格式

    Args:
        username: 用户名

    Returns:
        Tuple[bool, str]: (是否有效, 错误信息)
    """
    if not username:
        return False, "用户名不能为空"

    if len(username) < 2:
        return False, "用户名长度不能少于2个字符"

    if len(username) > 20:
        return False, "用户名长度不能超过20个字符"

    # 检查是否包含不允许的字符
    # 允许中文、英文、数字、下划线
    if not re.match(r"^[\u4e00-\u9fa5a-zA-Z0-9_]+$", username):
        return False, "用户名只能包含中文、英文、数字和下划线"

    return True, ""


def generate_uid(username: str) -> str:
    """
    根据用户名生成user_id

    Args:
        username: 用户名

    Returns:
        str: 生成的user_id
    """
    # 1. 基本清理
    username = username.strip()

    # 2. 转换为拼音（如果包含中文）
    uid = to_pinyin(username)

    # 3. 处理特殊字符，只保留字母、数字和下划线
    uid = re.sub(r"[^a-zA-Z0-9_]", "", uid)

    # 4. 确保不以数字开头
    if uid and uid[0].isdigit():
        uid = "u" + uid

    # 5. 如果为空或太短，使用默认前缀
    if len(uid) < 2:
        uid = "user" + str(hash(username) % 10000).zfill(4)

    # 6. 长度限制
    if len(uid) > 20:
        uid = uid[:20]

    return uid.lower()


def generate_unique_uid(username: str, existing_uids: list[str]) -> str:
    """
    生成唯一的user_id，如果重复则添加数字后缀

    Args:
        username: 用户名
        existing_uids: 已存在的user_id列表

    Returns:
        str: 唯一的user_id
    """
    base_uid = generate_uid(username)

    # 如果不重复，直接返回
    if base_uid not in existing_uids:
        return base_uid

    # 如果重复，添加数字后缀
    counter = 1
    while True:
        candidate = f"{base_uid}{counter}"
        if candidate not in existing_uids:
            return candidate
        counter += 1

        # 防止无限循环
        if counter > 9999:
            # 使用时间戳作为后缀
            import time

            candidate = f"{base_uid}{int(time.time()) % 10000}"
            return candidate


def is_valid_phone_number(phone: str) -> bool:
    """
    验证手机号格式（支持中国大陆手机号）

    Args:
        phone: 手机号字符串

    Returns:
        bool: 是否为有效手机号
    """
    if not phone:
        return False

    # 移除空格和特殊字符
    phone = re.sub(r"[\s\-\(\)]", "", phone)

    # 中国大陆手机号格式：1开头，第二位是3-9，总共11位
    pattern = r"^1[3-9]\d{9}$"

    return bool(re.match(pattern, phone))


def normalize_phone_number(phone: str) -> str:
    """
    标准化手机号格式

    Args:
        phone: 原始手机号

    Returns:
        str: 标准化后的手机号
    """
    if not phone:
        return ""

    # 移除所有非数字字符
    phone = re.sub(r"\D", "", phone)

    # 如果是中国大陆手机号，确保格式正确
    if len(phone) == 11 and phone.startswith("1"):
        return phone

    return phone
