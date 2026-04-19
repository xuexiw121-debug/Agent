import json
import re
import time

from dashscope import Generation


def run_health_check(dashscope_api_key: str, model_name: str) -> tuple[bool, str]:
    if not dashscope_api_key:
        return False, "未读取到 DASHSCOPE_API_KEY"

    try:
        response = Generation.call(
            model=model_name,
            messages=[{"role": "user", "content": "仅回复: OK"}],
            result_format="message",
            temperature=0,
        )
        if response.status_code == 200:
            text = response.output.choices[0].message.content
            return True, f"连通成功，模型返回: {text}"
        return False, f"连通失败: {response.code} - {response.message}"
    except Exception as e:
        return False, f"连通异常: {e}"


def calculate_budget_allocation(total_budget: int, days: int) -> dict:
    if days <= 0:
        days = 1

    ratios = {
        "交通": 0.30,
        "住宿": 0.35,
        "餐饮": 0.18,
        "景点活动": 0.12,
        "机动": 0.05,
    }

    allocation = {k: int(total_budget * v) for k, v in ratios.items()}
    daily_food = int(allocation["餐饮"] / days)
    daily_activity = int(allocation["景点活动"] / days)
    daily_hotel = int(allocation["住宿"] / days)

    return {
        "total_budget": total_budget,
        "days": days,
        "allocation": allocation,
        "daily_hint": {
            "每日餐饮参考": daily_food,
            "每日活动参考": daily_activity,
            "每日住宿参考": daily_hotel,
        },
    }


def call_generation_with_retry(model_name: str, messages: list, temperature: float = 0.7, max_retries: int = 3):
    last_error = "未知错误"

    for attempt in range(max_retries):
        try:
            response = Generation.call(
                model=model_name,
                messages=messages,
                result_format="message",
                temperature=temperature,
            )
            if response.status_code == 200:
                return True, response
            last_error = f"{response.code} - {response.message}"
        except Exception as e:
            last_error = str(e)

        if attempt < max_retries - 1:
            time.sleep(1.2 * (2 ** attempt))

    return False, last_error


def extract_json_from_text(text: str):
    text = text.strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    fenced = re.search(r"```json\s*(\{[\s\S]*\})\s*```", text, re.IGNORECASE)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except Exception:
            pass

    object_match = re.search(r"(\{[\s\S]*\})", text)
    if object_match:
        try:
            return json.loads(object_match.group(1))
        except Exception:
            pass

    return None


def ensure_daily_highlights(data: dict) -> dict:
    daily_plan = data.get("daily_plan", [])
    if not isinstance(daily_plan, list):
        return data

    fallback_spots = ["城市地标", "本地博物馆", "人气步行街", "特色公园", "夜景观景点"]

    for day_item in daily_plan:
        highlights = day_item.get("highlights", [])
        if not isinstance(highlights, list):
            highlights = []

        cleaned = []
        seen = set()
        for h in highlights:
            name = str(h).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            cleaned.append(name)

        if len(cleaned) < 2:
            for spot in fallback_spots:
                if spot not in seen:
                    cleaned.append(spot)
                    seen.add(spot)
                if len(cleaned) >= 2:
                    break

        day_item["highlights"] = cleaned[:3]

    data["daily_plan"] = daily_plan
    return data


def generate_travel_plan(
    dashscope_api_key: str,
    model_name: str,
    destination: str,
    preferences: str,
    days: int,
    total_budget: int,
):
    if not dashscope_api_key:
        return None, "DashScope API 密钥未设置！请在 .env 文件或环境变量中设置 DASHSCOPE_API_KEY。"

    budget_info = calculate_budget_allocation(total_budget, days)

    prompt = f"""
    你是一位专业的AI旅行规划师。请根据以下信息，为我生成一份详细、个性化且引人入胜的旅行计划。

    **目的地**: {destination}
    **我的偏好和需求**: {preferences}
    **旅行天数**: {days} 天
    **总预算(人民币)**: {total_budget}
    **预算分配建议(JSON)**: {json.dumps(budget_info, ensure_ascii=False)}

    请严格返回 JSON，不要返回任何额外文字。JSON 结构如下：
    {{
      "overview": "一句话总览",
      "budget_summary": "预算策略说明",
      "daily_plan": [
        {{
          "day": 1,
          "theme": "主题",
          "highlights": ["亮点1", "亮点2"],
          "food": "餐饮建议",
          "transport": "交通建议",
          "estimated_cost": 500
        }}
      ],
      "tips": ["注意事项1", "注意事项2"]
    }}

    关键约束：
    1) daily_plan 的天数必须等于 {days}。
    2) 每一天 highlights 必须是 2-3 个具体景点（不能只有 1 个）。
    3) 每天景点不要重复，且尽量选择同一区域，便于一天内串联。
    4) highlights 中不要出现“自由活动/休息”这类非景点词。
    5) 景点名称必须尽量使用地图可检索的标准名（如“故宫博物院”，避免“故宫附近”这类模糊写法）。
    6) 优先输出有明确地理实体的点位（博物馆/公园/古镇/广场/景区）。
    """

    ok, result = call_generation_with_retry(
        model_name=model_name,
        messages=[
            {"role": "system", "content": "你是一位顶级的 AI 旅行规划助手。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.7,
        max_retries=3,
    )

    if not ok:
        return None, f"调用 DashScope API 失败: {result}"

    raw_text = result.output.choices[0].message.content
    parsed = extract_json_from_text(raw_text)
    if parsed is None:
        return {
            "budget": budget_info,
            "raw": raw_text,
            "structured": None,
        }, None

    parsed = ensure_daily_highlights(parsed)
    return {
        "budget": budget_info,
        "raw": raw_text,
        "structured": parsed,
    }, None
