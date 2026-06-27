"""Extract forum-specific high-frequency ngrams from posts+comments."""
import sqlite3, re, collections, sys

db = sqlite3.connect(r"D:\temp\RUCxiaolaba-Advanced-Search\data\posts.db")

counter = collections.Counter()
total_chars = 0

rows = db.execute("SELECT content FROM posts UNION ALL SELECT detail FROM comments")
for (text,) in rows:
    if not text:
        continue
    cleaned = re.sub(r'[^一-鿿]', '', text)
    total_chars += len(cleaned)
    for n in (2, 3, 4):
        for i in range(len(cleaned) - n + 1):
            counter[cleaned[i:i+n]] += 1

print(f"Total Chinese chars: {total_chars:,}")
print(f"Unique 2-4grams: {len(counter):,}")

# Common Chinese function words / high-freq chars (filter out)
common_chars = set(
    '的一是不了在有人我他这之以来个大们上个到说们和地也子时而你要会那可以出对生就者去还得自能下过如么然所把好方都起小看天手但样被只从里当家知前因很想头面意开用又道身长做信没见明问力体水经定部相本法心重间关事现前动外后正公门十路过合'
    '比加机全气各内平社高组表处通并提展及理党战员军产性情文已最度化物利老新民同工两或实进主等此政学日社义事制度发'
    '学们成些中可时没为然还对能来么去会自那多只己到得现所起如种点用'
)

# Course/frequent terms to exclude
stop_ngrams = {
    '高数','数学分析','高等代数','微积分','线性代数','概率论','数理统计','宏观','微观','计量','会计',
    '金融','管理','经济','政治','哲学','历史','文学','英语','法语','日语','德语','体育','计算机','编程',
    'Python','C语言','数据结构','算法','物理','化学','生物','量子','统计','数分','高代','线代','概统',
    '经济学','金融学','会计学','管理学','财政学','税收','保险','审计','货币','银行','证券','投资',
    '马克思','毛概','思政','思修','近代史','马原','形策','军事','形势与政策',
    '社会学','新闻','传播','法律','法学','国际关系','外交','公共管理','人力资源','市场营销','工商管理',
    '期中','期末','考试','论文','作业','实验','报告','PPT','答辩','绩点','学分','选课','退课','补选',
    '交换','暑校','保研','考研','出国','留学','托福','雅思','GRE','GMAT',
    '实习','工作','就业','面试','简历','招聘','offer','工资','薪水','待遇',
    '因为我','因为我','是不是','有没有','不知道','我觉得','感觉','应该','可能','可以',
    '什么','怎么','为什么','怎么办','怎么样','一个','这个','那个','哪个','如果',
    '但是','因为','所以','虽然','然后','而且','或者','还是','已经','一定',
    '不会','不能','不要','没有','还是','也是','都是','还有','只是',
    '每天','今天','明天','昨天','现在','以后','以前','一直','已经','第一次',
    '男朋友','女朋友','谈恋爱','在一起','分手','表白','喜欢','爱情',
    '能不能','会不会','要不要','好不好','行不行','对不对','有没有人',
}

def is_noise(ngram):
    if ngram in stop_ngrams:
        return True
    if all(c in common_chars for c in ngram):
        return True
    # filter pure common-char combinations
    common_ratio = sum(1 for c in ngram if c in common_chars) / len(ngram)
    if common_ratio > 0.8:
        return True
    return False

filtered = [(k, v) for k, v in counter.most_common(8000) if not is_noise(k) and v >= 30]

print(f"\n{'='*60}")
print(f"Top 100 forum-specific ngrams (non-course, non-common):")
print(f"{'='*60}")
for i, (term, count) in enumerate(filtered[:100], 1):
    print(f"{i:3}. [{term}]  ({count:>8,})")

print(f"\nTop by length:")
for length in (2, 3, 4):
    subset = [(k,v) for k,v in filtered if len(k)==length][:15]
    print(f"\n--- {length}-gram ---")
    for term, count in subset:
        print(f"  [{term}]  ({count:,})")
