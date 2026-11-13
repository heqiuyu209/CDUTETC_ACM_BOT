"""
算法竞赛信息抓取脚本（ACM / 百度之星 / 蓝桥杯 / 传智杯 等）
================================================

【流程】用户只输入一句话（如“百度之星”），脚本自动完成下面 4 步：

  1. 重写层 rewrite_layer
     让大模型把口语化的输入改写成适合搜索引擎的 Query，
     例如 “百度之星” -> “百度之星 程序设计大赛 官网 官方通知”。

  2. 搜索层 search
     调用搜索 API 拿到候选网页（含标题 + URL）。

  3. 深挖层 get_all_links_playwright
     用 Playwright 打开排名第一的搜索结果页，抓出页面里所有 <a> 链接，
     再让大模型从这些链接里挑出最可能含“比赛规则 / 报名 / 奖金 / 公告”的
     子链接（最多 5 个），和主网页一起作为待抓取列表。

  4. 抓取 + 分析分析层 analysis_layer / ai_analysis_layer
     逐个 URL 调用正文抽取 API 拿纯文本（并行），
     最后把所有正文交给大模型，按固定 JSON 模板输出赛事信息
     （比赛名称、报名起止时间、奖金、官网等）。

  返回值：一个 dict（已解析好的结构化数据）；任何一步失败返回 None 或错误说明。
"""

import json
import os
import sys
import logging
import requests
from concurrent.futures import ThreadPoolExecutor  # 并发抓取用
from lxml import etree
from openai import OpenAI
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from urllib.parse import urljoin, urlparse

load_dotenv()

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('{levelname} - {message}', style='{'))
    logger.addHandler(handler)


def get_config(name: str) -> str:
    """读取环境变量，缺失时抛出明确错误，而不是把 None 传给接口"""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"缺少环境变量 {name}，请在 .env 中配置（参考文件开头的使用说明）"
        )
    return value


# 全局只建一次 client：每次调用都 new 一个会反复创建连接池
_client = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=get_config("DEEPSEEK_API_KEY"),
            base_url="https://api.deepseek.com",
        )
    return _client


def deepseek_chat(system_default_prompt: str, query: str | list[str]):
    """调用 DeepSeek 的 Chat 接口"""
    content = '\n'.join(query) if isinstance(query, list) else query

    response = get_client().chat.completions.create(
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        messages=[
            {"role": "system", "content": system_default_prompt},
            {"role": "user", "content": content},
        ],
        stream=False,
        extra_body={"thinking": {"type": "disabled"}},
    )
    return response.choices[0].message.content


# 单次抓取正文最多保留多少字符，避免超上下文
MAX_CONTENT_CHARS = 120_000
# 正文少于此长度视为反爬/无效页面
MIN_CONTENT_CHARS = 200


def input_layer(query: str):
    """输入层，接收用户输入"""
    logger.info(f"开始处理: {query}")

    searches = search(rewrite_layer(query))

    if len(searches) <= 0:
        logger.warning("未找到相关网页，终止任务")
        return None

    url = searches[0].get("url")
    if not url:
        logger.warning("首个搜索结果无有效链接")
        return None

    # 只对排名第一的官网主页做“深挖”
    logger.info(f"正在挖掘深层链接: {url}")
    extra_links = get_all_links_playwright(url)

    # 这里按 URL 去重，主网页放第一个，保证分析时它排在最前面
    seen = set()
    targets = []

    for item in searches + extra_links:
        u = item.get("url")
        if u and u not in seen:
            seen.add(u)
            targets.append(item)
    logger.info(f"计划抓取 {len(targets)} 个页面")

    return analysis_layer(targets)


def rewrite_layer(query: str):
    """重写层，对输入进行重写"""
    improve_prompt = f"""
    你是一个专业的搜索引擎查询优化专家。

    用户想搜索某个比赛、赛事或活动，核心目标是找到该比赛或活动的官方网站，以及官方发布的信息。

    请将用户输入改写为适合搜索引擎的专业查询词。如果用户输入已经很明确，携带具体的链接需要保留链接，则直接输出该输入。

    改写规则：
    1. 提取比赛、赛事或活动的准确名称。
    2. 优先搜索比赛主办方、承办方、官方组织机构的官方网站。
    3. 必须包含以下关键词中的适当组合：
       官网、官方网站、主办方、官方通知、报名入口。
    4. 如果用户想找报名信息，增加报名入口、报名通知等关键词。
    5. 如果用户想找比赛时间，增加比赛时间、赛程安排等关键词。
    6. 如果用户明确想找最新消息，可以增加当前年份：2026。
    7. 排除非官方信息来源，避免将培训机构、个人博客、论坛、新闻转载网站作为搜索目标。
    8. 不要添加与用户需求无关的关键词。
    9. 删除“帮我找一下”“我想知道”等口语化表达。
    10. 直接输出改写后的纯文本，不要解释，不要添加引号。
    
    用户输入：{query}

    改写后的Query：
    """
    res = deepseek_chat(improve_prompt, "请按要求改写上面的用户输入。")
    res = res.strip().strip('"“”')
    logger.info(f"重写查询词: {res}")
    return res


def search(query: str) -> list[dict[str, str]]:
    """搜索层"""
    response = requests.post(
        get_config("DEEPSEEK_SEARCH_API"),
        headers={"Authorization": "Bearer " + get_config("DEEPSEEK_API_KEY")},
        json={"query": query, "count": 5, "need_content": True},
        timeout=30,
    )
    response.raise_for_status()

    raw_results = response.json().get("results", [])
    results = [
        {"title": r.get("title", ""), "url": r.get("url", "")}
        for r in raw_results
        if r.get("url")
    ]
    logger.info(f"搜到主网页: {len(results)} 个")
    return results


def parse_html(html: str, base_url: str):
    """解析层，解析 HTML 获取所有链接"""
    tree = etree.HTML(html)
    raw_links = tree.xpath('//a[@href]')   # 找到具有 href 属性的 a 标签

    links = []
    seen = set()
    for a in (raw_links or []):
        link = (a.get("href") or "").strip()
        if not link or link.startswith(("javascript:", "#", "mailto:", "tel:", "data:")):
            continue
        full = urljoin(base_url, link)
        if urlparse(full).scheme not in ("http", "https"):
            continue
        if full in seen:
            continue
        seen.add(full)
        text = "".join(a.itertext()).strip()[:40]  # 链接文字，最多 40 字
        links.append(f"{text} | {full}" if text else full)
    return links


def get_all_links_playwright(url: str):
    """使用 Playwright 获取所有链接，并用 AI 筛选"""
    if not url:
        return []

    html = ""
    # 浏览器可能跟随重定向，相对链接必须以跳转后的地址为基准解析
    final_url = url
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)
            final_url = page.url or url
            html = page.content()
        except Exception as e:
            logger.error(f"网页加载失败: {e}")
            return []
        finally:
            browser.close()

    all_links = parse_html(html, final_url)
    if not all_links:
        return []

    system_default_prompt = """
    你是一个数据筛选助手。我会给你一个网页里的所有超链接，格式为「链接文字 | URL」。
    请帮我挑出最有可能包含【比赛规则、报名详情、奖金说明、官方公告、赛程时间】的链接。
    最多挑选 5 个；如果 URL 明显是登录页、广告、外站，请跳过。

    请严格返回 JSON 格式，不要包含多余解释：
    {"useful_links": [{"title": "string", "url": "string"}]}
    """

    try:
        response_str = deepseek_chat(system_default_prompt, all_links[:150])
        clean_json = response_str.replace("```json", "").replace("```", "").strip()
        useful = json.loads(clean_json).get("useful_links", [])
        valid = [
            {"title": u.get("title", ""), "url": u["url"]}
            for u in useful
            if isinstance(u, dict) and u.get("url", "").startswith(("http://", "https://"))
        ]
        logger.info(f"筛选出关键链接: {len(valid)} 个")
        return valid
    except Exception as e:
        logger.error(f"链接解析失败，放弃扩展: {e}")
        return []


def analysis_layer(searches: list[dict[str, str]]):
    """分析层，进行数据的抓取"""
    datas = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(fetch_with_url, searches))

    for url, content in results:
        if content and len(content) >= MIN_CONTENT_CHARS:
            # 长文本截断，避免传给大模型时超出上下文
            if len(content) > MAX_CONTENT_CHARS:
                logger.warning(f"正文过长已截断: {url} ({len(content)} 字符)")
                content = content[:MAX_CONTENT_CHARS]
            # 加上来源 URL，模型提取“官方网址”时有依据
            datas.append(f"【页面来源】{url}\n{content}")
        elif content:
            logger.warning(f"正文过短疑似反爬，丢弃: {url} ({len(content)} 字符)")
        else:
            logger.warning(f"抓取为空: {url}")

    if not datas:
        logger.warning("所有页面抓取失败，无法分析")
        return None

    logger.info(f"共抓取有效网页: {len(datas)} 个")
    return ai_analysis_layer(datas)


def fetch_with_url(item: dict[str, str]):
    """给线程池用的包装：返回 (url, content)"""
    return item.get("url", ""), fetch(item.get("url", ""))


def fetch(url: str):
    """抓取层"""
    try:
        response = requests.post(
            get_config("DEEPSEEK_FETCH_API"),
            headers={"Authorization": "Bearer " + get_config("DEEPSEEK_API_KEY")},
            json={"url": url},
            timeout=15
        )
        response.raise_for_status()
        return response.json().get("content", "")
    except Exception as e:
        logger.debug(f"抓取失败 {url}: {e}")
        return ""


def ai_analysis_layer(datas: list[str]):
    """AI分析层"""
    system_default_prompt = """
    你是一个专业的赛事数据提取专家。你的任务是从用户提供的网页长文本中，提取出关于【算法/程序设计比赛】的所有核心信息。
    这些比赛通常是 ACM-ICPC 系列赛、百度之星、蓝桥杯、Codeforces、牛客多校、CCPC、各大厂编程竞赛等。

    请严格按照以下 JSON 格式输出，不要包含任何多余的说明文字、不要包含 markdown 代码块标记。
    {
        "比赛名称": "提取完整的比赛官方名称",
        "比赛简介": "用150字以内总结",
        "主办方": "举办机构",
        "报名开始时间": "格式：YYYY-MM-DD，未提及填 null",
        "报名截止时间": "格式：YYYY-MM-DD，未提及填 null",
        "比赛时间": "未提及填 null",
        "参赛对象": "未提及填 null",
        "参赛形式": "个人赛 或 团队赛",
        "比赛类型": "线上赛 / 线下赛 / 线上线下结合，未提及填 null",
        "组别": "如 专业组/高校组/公开组/新手组，未提及填 null",
        "比赛奖金/奖品": "未提及填 null",
        "官方网址": "未提及填 null",
        "比赛赛道/组别": "未提及填 null",
        "联系方式": "未提及填 null"
    }

    【提取规则】：
    1. 未提及必须填 null，禁止捏造，必须返回合法 JSON。
    2. 同一信息在多个页面中冲突时，优先采信标注为“页面来源”且看起来最像官网的那一页。
    3. 报名截止时间、比赛时间这类关键日期，请尽量从原文中直接摘抄，不要推算。
    """

    logger.info("开始 AI 提取...")
    result = deepseek_chat(system_default_prompt, datas)

    if not result:
        return None
    try:
        clean_json = result.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_json)
        logger.info("提取完成（JSON 解析成功）")
        return data
    except json.JSONDecodeError:
        logger.warning("模型返回的不是合法 JSON，返回原始文本")
        return result


def web_scraping(query: str):
    return input_layer(query)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    for kw in ["https://www.boxuegu.com/match/"]:
        print(f"\n===== {kw} =====")
        result = web_scraping(kw)
        if isinstance(result, dict):
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(result)