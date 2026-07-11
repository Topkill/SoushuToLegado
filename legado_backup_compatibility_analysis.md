# 小说 APK 备份转 legado-E 备份兼容性分析

## 分析范围

目标仓库：

- `C:\Users\delll\Desktop\jadx\legado-E`
- 当前提交：`8b8de45d7 [日志]：更新日志`

源 APK 反编译目录：

- `C:\Users\delll\Desktop\jadx\sources`
- 已有源端备份分析：`C:\Users\delll\Desktop\jadx\backup_restore_analysis.md`

本阶段只分析备份格式、恢复逻辑和数据兼容性，不实现转换器。

## 结论概览

`legado-E` 的备份格式和前一个小说 APK 完全不同：

- 小说 APK：
  - 备份是普通 zip，但 zip entry 被映射成 `1.tag`、`2.tag` 等。
  - `_names.list` 保存真实路径。
  - 内容是应用私有目录的原始文件、SQLite 数据库、SharedPreferences、本地书文件等。
  - 本质是“文件级备份”。

- `legado-E`：
  - 备份是普通 zip。
  - zip 根目录下放固定文件名的 JSON/XML。
  - 主要从 Room 数据库导出为 JSON，再恢复时逐个 JSON 导入。
  - 本质是“数据模型级备份”。

所以转换器不能把源 APK 的 `.tag` 文件改名后直接塞给 `legado-E`。正确做法是：

1. 解析源备份 `_names.list`，还原每个 `.tag` 的真实身份。
2. 找到源 APK 的 SQLite 数据库。
3. 从源表读取书架、书籍信息、书源、TTS 等数据。
4. 按 `legado-E` 当前实体结构生成 `bookshelf.json`、`bookSource.json`、`httpTTS.json` 等目标文件。
5. 把这些目标文件打成 `legado-E` 可恢复的 zip。

## legado-E 备份格式

核心代码：

- `legado-E/app/src/main/java/io/legado/app/help/storage/Backup.kt`
- `legado-E/app/src/main/java/io/legado/app/help/storage/Restore.kt`
- `legado-E/app/src/main/java/io/legado/app/help/storage/BackupConfig.kt`
- `legado-E/app/src/main/java/io/legado/app/help/storage/BackupAES.kt`
- `legado-E/app/src/main/java/io/legado/app/utils/compress/ZipUtils.kt`

备份文件名：

- 默认：`backupyyyy-MM-dd.zip`
- 配置了设备名：`backupyyyy-MM-dd-设备名.zip`
- 如果启用“只保留最新备份”：`backup.zip`

zip 本身不加密，使用 `java.util.zip.ZipOutputStream` 普通压缩。

zip 内没有 `_names.list`，也没有 `.tag` 映射。备份文件直接以真实文件名放在 zip 根目录。

`Backup.kt` 中声明的目标文件名如下：

```text
bookshelf.json
bookmark.json
bookGroup.json
bookSource.json
rssSources.json
rssStar.json
replaceRule.json
readRecord.json
searchHistory.json
sourceSub.json
txtTocRule.json
httpTTS.json
keyboardAssists.json
dictRule.json
servers.json
directLinkUploadRule.json
readConfig.json
shareReadConfig.json
themeConfig.json
coverRule.json
config.xml
videoConfig.xml
```

其中，列表类数据只有在表非空时才会生成 JSON；文件缺失时恢复逻辑会跳过，不会报错。

特殊加密点：

- zip 文件整体不加密。
- `servers.json` 内容会尝试用 `BackupAES` 加密。
- `config.xml` 中的 WebDAV 密码会单独加密。
- `BackupAES` 密钥来自 `MD5(LocalConfig.password)` 的前 16 字节。

转换器通常不需要生成 `servers.json` 或 WebDAV 密码配置。

## legado-E 恢复逻辑

恢复入口：

```text
Restore.restore(context, uri)
  -> ZipUtils.unZipToPath(...)
  -> restoreLocked(Backup.backupPath)
  -> restore(path)
```

恢复时逐个读取固定文件名：

- `bookshelf.json` -> `Book`
- `bookmark.json` -> `Bookmark`
- `bookGroup.json` -> `BookGroup`
- `bookSource.json` -> `BookSource`
- `rssSources.json` -> `RssSource`
- `rssStar.json` -> `RssStar`
- `replaceRule.json` -> `ReplaceRule`
- `searchHistory.json` -> `SearchKeyword`
- `sourceSub.json` -> `RuleSub`
- `txtTocRule.json` -> `TxtTocRule`
- `httpTTS.json` -> `HttpTTS`
- `dictRule.json` -> `DictRule`
- `keyboardAssists.json` -> `KeyboardAssist`
- `readRecord.json` -> `ReadRecord`

导入策略大多是 `OnConflictStrategy.REPLACE`，相同主键会覆盖。

恢复 `bookshelf.json` 时有两个额外行为：

- 本地书籍如果开启了“忽略本地书”，会跳过。
- 本地书籍恢复后会重新计算封面路径：`LocalBook.getCoverPath(book)`。

重要限制：

- `Restore.kt` 不读取 `chapters` 表。
- `Restore.kt` 不读取正文缓存。
- `Restore.kt` 不读取 `cookies` 表。
- 即使目标数据库有这些表，官方备份恢复流程也不会处理它们。
- zip 中额外放 `chapters.json` 或正文缓存文件，目标 App 也会忽略。

## legado-E 核心目标数据模型

### 书架 books -> bookshelf.json

实体：`Book`

关键字段：

```text
bookUrl              主键；网络书是详情页 URL，本地书是文件路径
tocUrl               目录页 URL
origin               书源 URL；本地书默认为 loc_book
originName           书源名称或本地文件名
name                 书名
author               作者
kind                 分类
customTag            用户自定义分类
coverUrl             封面 URL
customCoverUrl       用户自定义封面
intro                简介
customIntro          用户自定义简介
charset              本地书字符集
type                 书籍类型位标记
group                分组位标记
latestChapterTitle   最新章节标题
lastCheckTime        最近检查时间
lastCheckCount       最近发现的新章节数
totalChapterNum      章节总数
durChapterTitle      当前章节名
durChapterIndex      当前章节序号
durChapterPos        当前章节内字符位置
durChapterTime       最近阅读时间
wordCount            字数
canUpdate            是否刷新书架时更新
order                手动排序
originOrder          书源排序
variable             自定义变量 JSON
readConfig           单书阅读配置
syncTime             同步时间
```

类型常量：

```text
BookType.text    = 8
BookType.audio   = 32
BookType.image   = 64
BookType.webFile = 128
BookType.local   = 256
BookType.localTag = "loc_book"
```

### 书源 book_sources -> bookSource.json

实体：`BookSource`

关键字段：

```text
bookSourceUrl
bookSourceName
bookSourceGroup
bookSourceType
bookUrlPattern
customOrder
enabled
enabledExplore
jsLib
enabledCookieJar
concurrentRate
header
loginUrl
loginUi
loginCheckJs
coverDecodeJs
bookSourceComment
variableComment
lastUpdateTime
respondTime
weight
exploreUrl
exploreScreen
ruleExplore
searchUrl
ruleSearch
ruleBookInfo
ruleToc
ruleContent
ruleReview
eventListener
customButton
```

书源类型：

```text
0 文本
1 音频
2 图片
3 只提供下载服务的网站
4 视频
```

新模型把旧模型中的扁平规则字段折叠成嵌套对象：

- `ruleExplore: ExploreRule`
- `ruleSearch: SearchRule`
- `ruleBookInfo: BookInfoRule`
- `ruleToc: TocRule`
- `ruleContent: ContentRule`

仓库已有 `ImportOldData.fromOldBookSource()`，它正好提供旧字段到新字段的转换算法。转换器应该参考这段逻辑生成新版 `bookSource.json`，不要直接把旧 `BOOK_SOURCE` 表按原字段导出。

### 分组 book_groups -> bookGroup.json

实体：`BookGroup`

关键字段：

```text
groupId
groupName
cover
order
enableRefresh
show
bookSort
onlyUpdateRead
```

内置分组：

```text
-1  全部
-2  本地
-3  音频
-4  网络未分组
-5  本地未分组
-6  视频
-11 更新失败
```

用户自定义分组使用正数 `groupId`。`Book.group` 是 Long 位标记，可以同时属于多个分组。

### HTTP TTS -> httpTTS.json

实体：`HttpTTS`

关键字段：

```text
id
name
url
contentType
concurrentRate
loginUrl
loginUi
header
jsLib
enabledCookieJar
loginCheckJs
lastUpdateTime
```

源 APK 的 HTTP TTS 表字段较少，只能部分填充。

## 源 APK 备份中可用的数据

源 APK 本地备份是文件级 zip，解压后只有：

```text
1.tag
2.tag
...
_names.list
```

`_names.list` 第 N 行对应 `N.tag` 的真实路径。`.tag` 不是文本格式，有些打开乱码是正常的，因为里面可能是 SQLite、图片、epub、书籍原文或其它二进制文件。

源 APK 备份会包含应用私有目录中的未过滤持久化文件，所以转换器需要从 `.tag` 中识别 SQLite 数据库。可用策略：

1. 读取 `_names.list` 建立序号到真实路径的映射。
2. 对每个 `.tag` 检查文件头是否为 SQLite。
3. 打开 SQLite，检查是否存在这些表：
   - `BOOK_SHELF`
   - `BOOK_INFO_BEAN`
   - `BOOK_SOURCE`
   - `BOOK_CHAPTER`
   - `BOOK_CONTENT_BEAN`
   - `COOKIE_BEAN`
   - `HTTP_TTS`
   - `CACHE`

已确认源端核心表：

### BOOK_SHELF

源代码：

- `sources/com/flyersort/source/gen/BookShelfDao.java`

字段：

```text
NOTE_URL             主键，书籍 URL
DUR_CHAPTER          当前章节序号
DUR_CHAPTER_PAGE     当前页/位置
FINAL_DATE           最近阅读时间
HAS_UPDATE           是否有更新
NEW_CHAPTERS         新章节数
TAG                  书源 URL 或本地标识
SERIAL_NUMBER        排序
FINAL_REFRESH_DATA   最近刷新时间
GROUP                分组
DUR_CHAPTER_NAME     当前章节名
LAST_CHAPTER_NAME    最新章节名
CHAPTER_LIST_SIZE    章节数量
CUSTOM_COVER_PATH    自定义封面
ALLOW_UPDATE         是否允许更新
USE_REPLACE_RULE     是否使用净化规则
VARIABLE             变量
REPLACE_ENABLE       替换开关
```

### BOOK_INFO_BEAN

源代码：

- `sources/com/flyersort/source/gen/BookInfoBeanDao.java`

字段：

```text
NAME
TAG
NOTE_URL             主键，书籍 URL
CHAPTER_URL
FINAL_REFRESH_DATA
COVER_URL
AUTHOR
INTRODUCE
ORIGIN
CHARSET
BOOK_SOURCE_TYPE
KIND
WORD_COUNT
LATEST_CHAPTER_TITLE
```

### BOOK_SOURCE

源代码：

- `sources/com/flyersort/source/gen/BookSourceDao.java`

字段包括：

```text
BOOK_SOURCE_URL
BOOK_SOURCE_NAME
BOOK_SOURCE_GROUP
BOOK_SOURCE_TYPE
LOGIN_URL
LAST_UPDATE_TIME
FROM
SERIAL_NUMBER
WEIGHT
ENABLE
RULE_FIND_*
RULE_SEARCH_*
RULE_BOOK_*
RULE_CHAPTER_*
RULE_CONTENT_*
HTTP_USER_AGENT
```

这张表与 `legado-E` 的旧格式导入逻辑高度匹配。

### BOOK_CHAPTER

源代码：

- `sources/com/flyersort/source/gen/BookChapterDao.java`

字段：

```text
TAG
NOTE_URL
INDEX
URL
TITLE
START
END
```

这部分是源 APK 的章节目录数据，但目标官方备份不会导入章节表。

### BOOK_CONTENT_BEAN

源代码：

- `sources/com/flyersort/source/gen/BookContentBeanDao.java`

字段：

```text
NOTE_URL
DUR_CHAPTER_URL
DUR_CHAPTER_INDEX
DUR_CHAPTER_CONTENT
TAG
TIME_MILLIS
```

这部分是源 APK 的正文缓存，目标官方备份不会导入正文缓存。

### 书架与书籍详情的归属关系

> 纠正说明：这一段是早期兼容性假设，不适用于当前 `com.flyersoft.seekbooks` 样本。  
> 该样本的真实书架/书籍归属在 `com.flyersoft.seekbooks/databases/mrbooks.db` 的 `books.favorite`，不是 `BOOK_SHELF` / `BOOK_INFO_BEAN`。

源 APK 备份里没有单独的“书架-书籍关系表”，也没有独立的 JSON 文件记录这层关系。它存在于备份包内某个 SQLite 数据库 `.tag` 文件中：

1. 先通过 `_names.list` 找到每个 `N.tag` 对应的真实路径。
2. 再扫描 `.tag` 文件头，定位包含 `BOOK_SHELF`、`BOOK_INFO_BEAN`、`BOOK_CHAPTER`、`BOOK_CONTENT_BEAN` 的 SQLite 数据库。
3. 在这个 SQLite 数据库里，书架项、书籍详情、章节列表、正文缓存都通过同一个 `NOTE_URL` 归属到同一本书。

核心关系如下：

```text
BOOK_SHELF.NOTE_URL      = BOOK_INFO_BEAN.NOTE_URL
BOOK_CHAPTER.NOTE_URL    = BOOK_SHELF.NOTE_URL
BOOK_CONTENT_BEAN.NOTE_URL = BOOK_SHELF.NOTE_URL
```

其中：

- `BOOK_SHELF.NOTE_URL` 是书架项主键，保存书是否在书架上、阅读进度、排序、分组、最近阅读时间、当前章节等状态。
- `BOOK_INFO_BEAN.NOTE_URL` 是书籍详情主键，保存书名、作者、简介、封面、目录 URL、来源名称、分类、字数等信息。
- `BOOK_CHAPTER.NOTE_URL` 表示这一章属于哪本书。
- `BOOK_CONTENT_BEAN.NOTE_URL` 表示这一段正文缓存属于哪本书。
- `BOOK_SHELF.TAG` 和 `BOOK_INFO_BEAN.TAG` 通常表示书源 URL 或本地标识，正常情况下应一致，但真正的归属主键仍是 `NOTE_URL`。

源代码里 `BookShelf` 虽然有运行时字段 `bookInfoBean`，但 GreenDAO 落库时并不是把 `BookInfo` 嵌入 `BOOK_SHELF` 表，而是分成 `BOOK_SHELF` 和 `BOOK_INFO_BEAN` 两张表保存，再用 `NOTE_URL` 关联。`BookShelf.getBookInfoBean()` 会把书架项里的 `noteUrl`、`tag`、当前章节名等同步到运行时 `BookInfo` 对象，这也印证了持久化关系的核心是 `NOTE_URL`。

转换到 `legado-E` 时，建议以 `BOOK_SHELF` 为主表做左连接：

```sql
SELECT *
FROM BOOK_SHELF s
LEFT JOIN BOOK_INFO_BEAN i
  ON i.NOTE_URL = s.NOTE_URL
```

然后把同一本书的书架状态和详情信息合并成一个 `Book` 对象写入 `bookshelf.json`。处理规则建议如下：

- `BOOK_SHELF` 有记录、`BOOK_INFO_BEAN` 也有记录：正常合并，生成一条 `Book`。
- `BOOK_SHELF` 有记录、`BOOK_INFO_BEAN` 缺失：仍可生成书架记录，但书名、作者、封面、简介等需要留空或从本地书伴随文件兜底，转换报告应记录缺失详情。
- `BOOK_INFO_BEAN` 有记录、`BOOK_SHELF` 缺失：这类数据可能只是搜索结果、详情缓存或历史残留，默认不应写入 `bookshelf.json`，除非后续明确提供“导入非书架书籍”的选项。
- `BOOK_CHAPTER` 和 `BOOK_CONTENT_BEAN` 可以用 `NOTE_URL` 识别归属，但官方 `legado-E` 备份恢复不导入章节和正文缓存，第一版转换器只应统计并在报告里说明跳过。

### COOKIE_BEAN

字段：

```text
URL
COOKIE
```

目标 `legado-E` 数据库也有 `cookies` 表，但 `Backup.kt` 和 `Restore.kt` 没有处理 Cookie，所以官方备份格式不兼容。

### HTTP_TTS

字段：

```text
_id
NAME
URL
```

目标 `HttpTTS` 字段更多，但 `id/name/url` 可直接迁移，其它字段用默认值或置空。

## 可转换数据

### 1. 书架书籍

兼容度：高。

源表：

- `BOOK_SHELF`
- `BOOK_INFO_BEAN`

目标文件：

- `bookshelf.json`

建议字段映射：

| 源字段 | 目标字段 | 说明 |
| --- | --- | --- |
| `BOOK_INFO_BEAN.NOTE_URL` 或 `BOOK_SHELF.NOTE_URL` | `bookUrl` | 主键，必须稳定 |
| `BOOK_INFO_BEAN.CHAPTER_URL` | `tocUrl` | 为空时可用 `bookUrl` |
| `BOOK_SHELF.TAG` | `origin` | 书源 URL；本地书通常是 `loc_book` |
| `BOOK_INFO_BEAN.ORIGIN` | `originName` | 书源名称或本地文件名 |
| `BOOK_INFO_BEAN.NAME` | `name` | 书名 |
| `BOOK_INFO_BEAN.AUTHOR` | `author` | 作者 |
| `BOOK_INFO_BEAN.KIND` | `kind` | 分类 |
| `BOOK_INFO_BEAN.COVER_URL` | `coverUrl` | 封面 |
| `BOOK_SHELF.CUSTOM_COVER_PATH` | `customCoverUrl` | 自定义封面路径 |
| `BOOK_INFO_BEAN.INTRODUCE` | `intro` | 简介 |
| `BOOK_INFO_BEAN.CHARSET` | `charset` | 本地书字符集 |
| `BOOK_INFO_BEAN.BOOK_SOURCE_TYPE` | `type` | `AUDIO` -> `BookType.audio`，本地书加 `BookType.local`，默认文本 |
| `BOOK_SHELF.LAST_CHAPTER_NAME` | `latestChapterTitle` | 可 fallback 到 `BOOK_INFO_BEAN.LATEST_CHAPTER_TITLE` |
| `BOOK_INFO_BEAN.FINAL_REFRESH_DATA` 或 `BOOK_SHELF.FINAL_REFRESH_DATA` | `lastCheckTime` | 最近检查时间 |
| `BOOK_SHELF.NEW_CHAPTERS` | `lastCheckCount` | 新章节数量 |
| `BOOK_SHELF.CHAPTER_LIST_SIZE` | `totalChapterNum` | 章节总数 |
| `BOOK_SHELF.DUR_CHAPTER_NAME` | `durChapterTitle` | 当前章节名 |
| `BOOK_SHELF.DUR_CHAPTER` | `durChapterIndex` | 当前章节序号 |
| `BOOK_SHELF.DUR_CHAPTER_PAGE` | `durChapterPos` | 章节内位置，目标语义是字符位置 |
| `BOOK_SHELF.FINAL_DATE` | `durChapterTime` | 最近阅读时间 |
| `BOOK_INFO_BEAN.WORD_COUNT` | `wordCount` | 字数 |
| `BOOK_SHELF.ALLOW_UPDATE` | `canUpdate` | 是否允许更新 |
| `BOOK_SHELF.SERIAL_NUMBER` | `order` | 手动排序 |
| `BOOK_SHELF.VARIABLE` | `variable` | 自定义变量 |
| `BOOK_SHELF.USE_REPLACE_RULE` | `readConfig.useReplaceRule` | 单书净化开关 |

注意：

- `legado-E` 的 `durChapterPos` 是“当前章节首行字符索引位置”。源 APK 的 `DUR_CHAPTER_PAGE` 名称像页码，但 `ImportOldData` 已直接映射到 `durChapterPos`，可以先沿用。
- 源 APK 的 `GROUP` 是 Int，目标是 Long 位标记。当前仓库的 `ImportOldData.fromOldBooks()` 没有映射旧分组，说明这部分不能盲目直接迁移，需要结合样本确认旧 `GROUP` 的含义。

### 2. 书源

兼容度：高，但必须做结构转换。

源表：

- `BOOK_SOURCE`

目标文件：

- `bookSource.json`

建议直接复刻 `ImportOldData.fromOldBookSource()` 的逻辑：

| 源字段 | 目标字段 |
| --- | --- |
| `bookSourceUrl` | `bookSourceUrl` |
| `bookSourceName` | `bookSourceName` |
| `bookSourceGroup` | `bookSourceGroup` |
| `bookSourceType == AUDIO` | `bookSourceType = 1` |
| 其它类型 | `bookSourceType = 0` |
| `ruleBookUrlPattern` | `bookUrlPattern` |
| `serialNumber` | `customOrder` |
| `enable` | `enabled` |
| `weight` | `weight` |
| `lastUpdateTime` | `lastUpdateTime` |
| `loginUrl` | `loginUrl` |
| `httpUserAgent` | `header`，包装成 `{"User-Agent":"..."}` |
| `ruleFindUrl` | `exploreUrl`，使用旧 URL 转新 URL 规则 |
| `ruleFind*` | `ruleExplore` |
| `ruleSearchUrl` | `searchUrl`，使用旧 URL 转新 URL 规则 |
| `ruleSearch*` | `ruleSearch` |
| `ruleBookInfoInit/ruleBookName/...` | `ruleBookInfo` |
| `ruleChapterUrl/ruleChapterList/...` | `ruleToc` |
| `ruleBookContent/ruleBookContentReplaceRegex/ruleContentUrlNext` | `ruleContent` |

规则字符串还要做旧规则到新规则的兼容处理：

- 单个 `#` 替换成 `##`
- 单个 `|` 替换成 `||`
- 部分 `&` 替换成 `&&`
- `searchKey` -> `{{key}}`
- `searchPage` -> `{{page}}`
- 老式 `@Header:{...}` 转成新版 URL 参数 JSON
- 老式 `url@body` 转成 `url,{"method":"POST","body":"..."}`

这些规则都在 `ImportOldData.toNewRule()`、`toNewUrl()`、`toNewUrls()` 中。

### 3. HTTP TTS

兼容度：中。

源表：

- `HTTP_TTS`

目标文件：

- `httpTTS.json`

可映射：

| 源字段 | 目标字段 |
| --- | --- |
| `_id` | `id` |
| `NAME` | `name` |
| `URL` | `url` |

目标缺失字段可用默认值：

```text
contentType = null
concurrentRate = "0"
loginUrl = null
loginUi = null
header = null
jsLib = null
enabledCookieJar = false
loginCheckJs = null
lastUpdateTime = 当前时间或 0
```

### 4. 本地书元数据

兼容度：中到低。

源 APK 会额外备份本地书文件及伴随文件：

```text
.sources
<书籍ID>/.name
<书籍ID>/.author
<书籍ID>/.chapters
<书籍ID>/.latestc
<书籍ID>/.url
<书籍ID>/.description
<书籍ID>/.tag
<书籍ID>/.varible
<书籍ID>/.cover
```

其中书名、作者、简介、最新章节、变量、封面等可补充到 `bookshelf.json`。

但目标 `legado-E` 官方备份不会打包本地书原文件。即使源备份里有本地书，转换后的目标 zip 也只能保存书籍记录中的路径，不能通过官方恢复流程把本地书文件一起恢复到设备。

如果后续要完整迁移本地书，有两个选择：

1. 生成官方 `legado-E` 备份 zip，同时输出一个独立的本地书文件目录，让用户手动放到目标设备固定路径。
2. 不只做备份转换器，而是做一个 `legado-E` 专用导入工具或改 App 恢复逻辑，让它能读取转换包里的本地书文件。

第一版建议先不迁移本地书原文件，只迁移书架记录和可用元数据。

## 部分兼容或需确认的数据

### 1. 分组

源字段：

- `BOOK_SHELF.GROUP`

目标字段：

- `Book.group`
- `bookGroup.json`

问题：

- 源 APK 当前只确认到书架表里有 Int 类型 `GROUP`。
- 暂未确认源 APK 是否另有“分组名称表”。
- `legado-E` 新版分组是 Long 位标记，不是普通 Int 序号。

建议：

- 第一版可以忽略分组，所有网络书进入“网络未分组”，本地书进入“本地未分组”。
- 如果样本备份中能找到分组名称配置，再建立 `bookGroup.json` 并映射 `Book.group`。

### 2. 替换/净化规则

源端已确认：

- `BOOK_SHELF.USE_REPLACE_RULE`
- `BOOK_SHELF.REPLACE_ENABLE`

目标端：

- `Book.readConfig.useReplaceRule`
- `replaceRule.json`

问题：

- 源 APK 中没有明确定位到与 `legado-E.replace_rules` 一一对应的替换规则表。
- 源代码里存在全局替换列表，但更像 SharedPreferences 或其它配置，不是当前已确认的 GreenDAO 表。

建议：

- 先迁移单书开关 `useReplaceRule`。
- `replaceRule.json` 暂不生成，除非在样本备份中确认规则本体来源。

### 3. TXT 目录规则

源端有 `TxtTocRule` 模型，目标端也有 `txtTocRules` 表。

问题：

- 暂未确认源端自定义 TXT 目录规则实际落在哪个数据库表或配置文件中。

建议：

- 第一版不生成 `txtTocRule.json`。
- 样本备份中若发现对应 JSON/表，再补充转换。

### 4. 阅读统计 readRecord

目标 `readRecord.json` 字段：

```text
deviceId
bookName
readTime
lastRead
```

源 APK 当前确认的是单书阅读进度，不是跨设备阅读时长统计。

建议：

- 不生成 `readRecord.json`。
- 书籍最近阅读时间已经通过 `Book.durChapterTime` 保留。

## 不兼容或不建议转换的数据

### 1. 章节目录

源数据：

- `BOOK_CHAPTER`

目标数据库有 `chapters` 表，但官方备份不导出也不导入。

结论：

- 不能通过官方 `legado-E` 备份 zip 迁移。
- 生成 `chapters.json` 也会被忽略。
- 恢复后章节列表应由书源或本地书重新解析。

### 2. 正文缓存

源数据：

- `BOOK_CONTENT_BEAN`

目标官方备份不处理正文缓存。

结论：

- 不转换。
- 恢复后需要重新联网加载或重新解析本地文件。

### 3. Cookie

源数据：

- `COOKIE_BEAN`

目标数据库有 `cookies` 表，但备份恢复不处理。

结论：

- 官方备份格式不兼容。
- 出于账号隐私和安全，也不建议默认转换 Cookie。

### 4. Cache 表

源数据：

- `CACHE`

目标官方备份不处理运行缓存。

结论：

- 不转换。

### 5. SharedPreferences 配置

源 APK 的 SharedPreferences 包含：

- UI 设置
- 设备适配
- 阅读器布局
- 账号/会员/隐私状态
- 广告 SDK 状态
- 默认书库路径

目标 `legado-E` 配置 key 不同。

结论：

- 不应整体迁移。
- 第一版建议不生成 `config.xml` 和 `videoConfig.xml`。
- 如后续需要，只挑选明确等价的阅读配置做白名单映射。

### 6. 账号、会员、广告、Bugly、第三方 SDK 数据

这些数据要么源备份过滤掉，要么属于源 App 私有状态。

结论：

- 不转换。

### 7. RSS、订阅、字典、键盘辅助、服务器配置

目标文件：

```text
rssSources.json
rssStar.json
sourceSub.json
dictRule.json
keyboardAssists.json
servers.json
directLinkUploadRule.json
coverRule.json
themeConfig.json
readConfig.json
shareReadConfig.json
```

源 APK 当前没有明确等价数据。

结论：

- 第一版不生成。

## 转换器建议方案

### 输入

源 APK 本地备份：

```text
ssds.backup
```

或用户手动选择的同格式备份文件。

### 输出

`legado-E` 官方可恢复 zip：

```text
backup-converted.zip
```

zip 根目录建议先只包含：

```text
bookshelf.json
bookSource.json
httpTTS.json
```

如果后续确认分组可映射，再增加：

```text
bookGroup.json
```

如果确认替换规则来源，再增加：

```text
replaceRule.json
```

### 处理流程

1. 解压源 `ssds.backup` 到临时目录。
2. 读取 `_names.list`。
3. 建立映射：

```text
1.tag -> _names.list 第 1 行真实路径
2.tag -> _names.list 第 2 行真实路径
...
```

4. 扫描 `.tag` 文件：
   - SQLite 文件：读取表名。
   - 文本/XML/JSON：暂存，供后续可选配置分析。
   - 图片/epub/txt/其它二进制：只记录，不默认放入目标 zip。

5. 定位源核心数据库：
   - 必须包含 `BOOK_SHELF`、`BOOK_INFO_BEAN` 或 `BOOK_SOURCE`。

6. 导出并转换：
   - `BOOK_SHELF` + `BOOK_INFO_BEAN` -> `bookshelf.json`
   - `BOOK_SOURCE` -> `bookSource.json`
   - `HTTP_TTS` -> `httpTTS.json`

7. 写转换报告：
   - 成功转换多少本书。
   - 成功转换多少个书源。
   - 跳过多少条章节目录。
   - 跳过多少条正文缓存。
   - 跳过多少条 Cookie。
   - 跳过多少个本地书文件。
   - 哪些书缺少 `BookInfo` 或缺少书源。

8. 使用普通 zip 打包目标 JSON 文件。

### 生成 JSON 的注意事项

`legado-E` 使用 Gson 且没有开启 `serializeNulls()`，所以官方备份会省略 null 字段。转换器可以省略 null，但这些字段建议显式输出安全值：

```text
Book.bookUrl
Book.tocUrl
Book.origin
Book.originName
Book.name
Book.author
Book.type
Book.group
Book.durChapterIndex
Book.durChapterPos
Book.durChapterTime

BookSource.bookSourceUrl
BookSource.bookSourceName
BookSource.bookSourceType
BookSource.customOrder
BookSource.enabled
BookSource.enabledExplore
BookSource.respondTime
BookSource.weight
```

原因：

- Kotlin 非空字段如果缺失，Gson 可能通过反射生成不安全的 null。
- 主键字段为空会导致恢复失败或覆盖错误。

## 第一版转换范围建议

建议第一版只做这些：

1. `bookshelf.json`
   - 书名、作者、URL、书源、简介、封面、最新章节、阅读进度、排序、变量、净化开关。

2. `bookSource.json`
   - 按 `ImportOldData.fromOldBookSource()` 逻辑完整转换旧书源。

3. `httpTTS.json`
   - 只迁移 `id/name/url`。

4. 转换报告
   - 清楚列出未转换数据。

第一版暂不做：

- 本地书文件打包。
- 章节目录。
- 正文缓存。
- Cookie。
- 源 App 配置。
- 账号/会员/广告数据。
- RSS/字典/服务器配置。
- 替换规则本体。
- TXT 目录规则。

## 待确认问题

实现前建议确认：

1. 是否接受第一版不迁移本地书原文件，只迁移书架记录？
2. 是否需要输出一个“本地书文件目录”作为 zip 之外的附加结果？
3. 是否忽略源 APK 的分组，还是等样本确认 `GROUP` 含义后再迁移？
4. 是否默认不迁移 Cookie、账号和隐私相关数据？
5. 是否要把转换报告作为单独 `.md` 或 `.json` 输出？

## 最终判断

可高质量转换的数据：

- 书架基本信息
- 阅读进度
- 书籍简介/封面/最新章节
- 书源及大部分规则
- HTTP TTS 基础配置

部分可转换但需确认的数据：

- 分组
- 替换/净化规则本体
- TXT 目录规则
- 本地书伴随元数据

不适合通过官方 `legado-E` 备份转换的数据：

- 章节目录
- 正文缓存
- Cookie
- Cache
- 本地书原文件
- 源 App 的 SharedPreferences 整体配置
- 账号、会员、广告、设备适配类数据

因此，转换器的合理目标不是“完整复刻源 App 私有目录”，而是“生成一个 `legado-E` 官方恢复逻辑能理解的书架/书源迁移包”。
