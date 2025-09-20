# config.py
"""
配置文件。所有可修改的配置都放在这里。
"""

API_VERSION = "1.0.0"
DB_FILE = "data.db"

# 抓取间隔（秒）：2 小时
FETCH_INTERVAL = 2 * 60 * 60

# 缓存 TTL（秒）
CACHE_TTL = 60

# RSS 列表与对应名称（长度相同）
# 示例，请替换为你自己的订阅
rss_links = [
    "https://leonxie.cn/rss.xml",
    "https://qwq.blue/rss.xml",
    "https://pinpe.top/rss.xml",
    "https://blog.yaria.top/rss.xml",
    "https://blog.leonxie.cn/rss.xml"
]
link_names = [
    "Leonxieの小窝",
    "Silvaire's Blog",
    "Pinpe的云端",
    "Ariasakaの小窝",
    "晓夜の后花园"
]

# Web 服务绑定
HOST = "0.0.0.0"
PORT = 8000

# run.py 使用的 pid / log 路径
PID_FILE = "server.pid"
LOG_FILE = "server.log"
