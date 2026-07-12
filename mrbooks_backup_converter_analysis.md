# 源 APK 本地备份到 legado-E 书架转换说明

## 当前结论

这份源 APK 测试备份的真实书架数据在：

```text
com.flyersoft.seekbooks/databases/mrbooks.db
```

对应解压目录中的文件是：

```text
24.db
```

`moon-db` 里虽然有 `BOOK_SHELF`、`BOOK_INFO_BEAN` 等表，但这份样本中这些表没有书架书籍数据。本转换器不再使用 `BOOK_SHELF` 路径，直接以 `mrbooks.db.books` 为唯一来源。

## _names.list 映射

源备份是普通 zip，但文件名被改成数字编号：

```text
N.tag / N.db
```

`_names.list` 第 N 行就是这个编号文件的真实路径。测试备份的关键行：

```text
23: com.flyersoft.seekbooks/databases/moon-db
24: com.flyersoft.seekbooks/databases/mrbooks.db
25: /sdcard/Books/Soushu/.Books/同归择天记(洛水渐清)/同归择天记.wbpub
...
42: /sdcard/Books/Soushu/.Books/择天记(徐Chang)/择天记.wbpub
```

`.tag` 打开乱码，是因为其中很多不是文本：有的是 SQLite 数据库，有的是 zlib 压缩后的文本。`.wbpub` 及其 `.name`、`.author`、`.chapters`、`.url`、`.description`、`.cover` 等伴随文件需要先 zlib 解压，再按 UTF-8/GBK 文本读取。

## 书架与分组

`mrbooks.db.books.favorite` 是每本书所属的书架/分组名。

源 APK 的书架顺序在：

```text
com.flyersoft.seekbooks/shared_prefs/shelf_names.txt
```

测试样本对应：

```text
19.tag
```

转换到 legado-E 时：

- 每个首次出现的 `favorite` 生成一个 legado 自定义分组。
- 分组顺序优先按 `shelf_names.txt`；如果某个 `favorite` 不在 `shelf_names.txt`，再按 `books._id` 首次出现顺序追加。
- `groupName` 直接使用 `favorite`。
- `groupId` 按 legado-E 的位标记规则生成：`1, 2, 4, 8...`。
- 每本书的 `Book.group` 写入它所属书架对应的 `groupId`。
- 如果 `favorite` 为空，目标 `groupName` 也为空，不写“未分组”之类的兜底名。
- 源自定义分组的 `bookSort` 保持 legado 默认值 `-1`。
- 每本书的 `order` 按源 `books._id` 排序后模拟 legado 新加入书架：写成 `-1, -2, -3...`。

legado-E 自带分组是负数 ID，例如 `全部(-1)`、`本地(-2)`、`网络未分组(-4)` 等；用户自己创建的书架分组使用正数位标记。代码里的 `BookGroupDao.getUnusedId()` 从 `1L` 开始，每次左移一位，所以新增分组就是 `1, 2, 4, 8...` 这种序列。

生成转换备份时，`bookGroup.json` 会写入 legado 自带负数分组，同时追加源 APK 对应的正数自定义分组。测试样本里的 `本地` 书架会生成 `本地(8)`，它和 legado 自带的 `本地(-2)` 不是同一个分组，不能混用。

测试样本转换结果：

```text
书架一   -> groupId 1 -> 择天记
书架二   -> groupId 2 -> 同归择天记
番茄小说 -> groupId 4 -> 我欲封天
本地     -> groupId 8 -> debug_log
```

## books 表字段

源表：

```text
books(
  _id,
  book,
  filename,
  lowerFilename,
  author,
  description,
  category,
  thumbFile,
  coverFile,
  addTime,
  favorite,
  downloadUrl,
  rate,
  bak1,
  bak2
)
```

当前第一版转换：

- `books` 表里的书架书籍和书籍元数据
- `shared_prefs/web_book_search` 里的书籍搜索历史

不转换章节正文、书源规则、Cookie、TTS、阅读设置和本地原文件。

## 两类本地相关书籍

### .wbpub 书源书籍

路径形如：

```text
/sdcard/Books/Soushu/.Books/书名(作者)/书名.wbpub
```

这类书是通过源 APK 的书源搜索后加入书架的书。它虽然在备份中表现为本地 `.wbpub` 文件，但本质是网络书籍的导出缓存。

伴随文件：

```text
.sources
sourceKey/.name
sourceKey/.author
sourceKey/.chapters
sourceKey/.latestc
sourceKey/.url
sourceKey/.description
sourceKey/.cover
```

转换规则：

- `bookUrl`：`sourceKey/.url`
- `tocUrl`：空字符串，源 `.wbpub` 伴随文件没有 legado 独立目录页 URL
- `origin`：`.wbpub` 解压后的书源 key，例如 `jjwxc`、`faloo`
- `originName`：`.sources` 中的书源名
- `name`：`sourceKey/.name`
- `author`：`sourceKey/.author`
- `intro`：`sourceKey/.description`
- `coverUrl`：`sourceKey/.cover`
- `latestChapterTitle`：`sourceKey/.latestc` 中 `序号*章节名` 的章节名
- `totalChapterNum`：`sourceKey/.chapters` 的有效行数
- `type`：`8`，即 legado-E 文本书

缺失字段按缺失输出，不从 `books` 表或其他伴随文件兜底。

### 真正导入的本地书

路径形如：

```text
/sdcard/Books/Soushu/debug_log.txt
```

这类是真正的 txt/epub 等本地文件。测试样本里 `debug_log.txt` 没有写入 `_names.list`，所以备份里只有 `books` 表中的元数据，没有原 txt 文件内容。

转换规则：

- `bookUrl`：`books.filename`
- `tocUrl`：空字符串，第一版不解析本地书目录规则
- `origin`：`loc_book`
- `originName`：文件名，例如 `debug_log.txt`
- `name`：`books.book`
- `author`：`books.author`，如果是 `(TXT)` 类型标记则写空
- `kind`：`books.category`，如果是 `(TXT)` 类型标记则缺失
- `coverUrl`：`books.coverFile`
- `type`：`264`，即 `8 | 256`，文本 + 本地
- `canUpdate`：`true`，与 legado 原生备份里的本地书字段保持一致

本地书同样不做字段兜底：例如 `books.book` 为空时不会用文件名生成书名。

## 搜索历史

源 APK 的书籍搜索历史在：

```text
com.flyersoft.seekbooks/shared_prefs/web_book_search
```

对应测试解压目录：

```text
17.tag
```

文件格式是一行一个搜索关键词，例如：

```text
我欲封天
择天记
搜神记
```

转换到 legado：

- 输出文件：`searchHistory.json`
- 目标实体：`SearchKeyword`
- `word`：源文件中的关键词
- `usage`：固定 `1`
- `lastUseTime`：源文件没有时间字段，转换时按源文件行顺序生成递减时间，用于保持搜索历史显示顺序

源 `history.txt` 不是书籍搜索历史。测试样本里的 `shared_prefs/history.txt` 是本地打开/导入过的文件路径。

## 阅读进度映射

源 APK：

```text
shared_prefs/positions10.xml
```

格式：

```text
文件路径小写 = "章节索引@分段#位置:百分比"
```

例子：

```text
/sdcard/books/soushu/.books/斗破苍穹(天蚕土豆)/斗破苍穹.wbpub = 12@0#0:0.0%
```

规则：

- 章节索引从 `0` 开始：读到第 1 章是 `0`，读到第 13 章是 `12`
- `durChapterIndex` <- 章节索引
- `durChapterPos` <- `#` 后的位置
- `durChapterTitle` <- `.chapters` 中对应索引的章节名
- `durChapterTime` 优先按 `history.txt` 最近打开顺序生成递减时间戳，使 legado “按阅读时间”排序与源一致：
  - `history[0]` / `lastFile` <- `lastReadTime`（若无则回退合理 base）
  - `history[1]` <- base - 1
  - `history[2]` <- base - 2
  - …
- `history.txt` 未出现的书：
  - `durChapterTime` <- `books.addTime`

## addTime 映射

源 APK：

```text
mrbooks.db.books.addTime
```

含义：加入书架时间。

legado `Book` **没有**同名或专用“加入时间”字段。现有时间字段语义是：

- `latestChapterTime`：最新章节标题更新时间
- `lastCheckTime`：最近一次更新/检查书籍信息的时间
- `durChapterTime`：最近一次阅读书籍的时间（打开正文时间）

本地书导入时，legado 会把文件时间写到 `latestChapterTime`；新建 `Book` 时另外两个时间字段也会落在创建时刻附近。

因此当前转换把源 `addTime` 映射为：

```text
latestChapterTime = books.addTime
lastCheckTime     = books.addTime
durChapterTime    = books.addTime
```

这表示“这本书在 addTime 这个时刻进入书架”，不是宣称已经找到真实上次阅读进度。

## 缺失字段策略

legado 恢复 `bookshelf.json` 时用普通 Gson，缺失字段不会可靠落到 Kotlin 默认值。

因此转换规则是：

- 有源数据：写源值
- 无源数据 + 可空可选：省略
  - 如 `readConfig`、`variable`、`charset`、`wordCount`、空的 `intro/kind/coverUrl`
- 无源数据 + 影响恢复语义：写合理显式值
  - `type`: 网络书 `8`，本地 txt `264`
  - `origin`: 网络书源 key，本地书 `loc_book`
  - `canUpdate`: `true`
  - `order`: `-1,-2,-3...`
  - `group`: 对应自定义分组位标记
  - `tocUrl`: 无目录页时 `""`
  - `lastCheckCount` / `durChapterIndex` / `durChapterPos` / `originOrder`: `0`
  - 时间字段：legado 没有单独的“加入时间”字段
  - 源 `books.addTime`（加入书架时间）映射到：
    - `latestChapterTime = addTime`
    - `lastCheckTime = addTime`
    - `durChapterTime = addTime`
  - 原因：legado 新建/加入书架时，这三项时间通常都会被写成创建时刻；源 APK 没有更精确的上次阅读/检查时间时，用 `addTime` 模拟“在该时刻加入书架”
  - 注意：这不是真实“上次阅读时间”映射；真实阅读进度另算

## 当前脚本


脚本路径：

```text
C:\Users\delll\Desktop\jadx\convert_bookshelf_backup.py
```

支持输入源 APK 备份 zip，也支持输入已解压目录：

```powershell
python .\convert_bookshelf_backup.py .\ssds.backup -o .\backup-converted.zip
python .\convert_bookshelf_backup.py .\com.flyersoft.seekbooks -o .\backup-converted.zip
```

输出 zip 根目录：

```text
bookshelf.json
bookGroup.json
searchHistory.json
```

测试样本输出：

```text
书籍: 4
自定义书架/分组: 4
legado内置分组: 6
搜索历史: 3
.wbpub书源书籍: 3
真正本地书籍: 1
```
