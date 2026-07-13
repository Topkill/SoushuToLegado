# 搜书大师 APK 本地备份与恢复逻辑分析

## 结论概览

基于`jadx`反编译`23.11`版本的分析：

该 APK 的备份不是 Android 系统自动备份。`resources/AndroidManifest.xml` 中 `android:allowBackup="false"`，应用自己在“关于/用户信息”页面实现了备份与恢复。

本地备份的核心行为是：把应用私有数据目录打成一个 zip 包，并在 `z7=true` 时额外尝试打包书架/最近阅读里的一些**外部书路径文件**及其伴随元数据。实测样本里，额外打进包的主要是 `Books/Soushu/.Books/**.wbpub`（网文书缓存）及 `.sources`、`$$书源/.name` 等伴随文件；用户导入的**本地书**（如 `/sdcard/Books/Soushu/debug_log.txt`）即使在书架上，也**未必**会被打包。备份文件默认落在外部存储的 `Books/Soushu/ssds.backup`，恢复时会解包覆盖到当前应用数据目录，并对部分设备相关配置做适配。

重点代码：

- `sources/com/flyersoft/WB/AboutAct.java`
  - `doBackup()`
  - `doBackup2()`
  - `doBackup3(boolean z6)`
  - `doRestore()`
  - `doRestore4(String str)`
  - `getBackupFile()`
  - `getRestoreFile()`
- `sources/com/flyersoft/components/t.java`
  - `j(String srcDir, String outFile, boolean z6, boolean z7)`：zip 打包入口
  - `h(...)`：递归打包内部数据目录
  - `w(ZipOutputStream)`：额外尝试打包外部书路径文件和伴随元数据（样本中主要是 `.wbpub`）
  - `r(String path)`：备份过滤器
  - `m(...) / u(...) / p(...) / o(...)`：`_names.list` 映射相关逻辑

## 本地备份入口链路

界面入口在 `AboutAct.doBackup()`。它弹出两个选项：

- 本地备份
- 云端备份

选择“本地备份”后调用链如下：

```text
AboutAct.doBackup()
  -> C0233.m18085(...)
  -> AboutAct.doBackup2()
  -> C0314.m31217(...)
  -> AboutAct.doBackup3()
  -> AboutAct.doBackup3(true)
  -> C0230.m17846(C0300.m29613(), getBackupFile(), true, true)
  -> components.t.j(srcDir, outFile, true, true)
```

`doBackup2()` 会先把已有备份重命名成旧文件：

```text
ssds.backup -> ssds.backup.old
```

然后再启动正式备份。

关键参数含义：

- `C0300.m29613()` 返回 `e.i8`
  - `e.i8` 在 `SeekBooksApplication` 中由 `ApplicationInfo.dataDir` 初始化
  - 即应用私有目录，典型路径类似 `/data/user/0/com.flyersoft.seekbooks`
- `getBackupFile()` 返回 `C0296.m29316() + "/ssds.backup"`
- `doBackup3(true)` 的 `true` 会传给打包函数的第四个参数 `z7`
  - `z7=true` 表示额外尝试打包书架/历史中的外部书路径文件及伴随元数据（样本中主要是 `.wbpub` 网文书缓存，不保证本地 txt）
- 第三个参数 `z6=true`
  - 表示启用 zip entry 名映射，真实路径写入 `_names.list`

因此，本地备份默认不是“只备份配置”，而是：

```text
应用私有数据目录
+（z7=true 时）书架/最近阅读中通过筛选且文件仍存在的外部书路径
+ 上述路径对应的伴随元数据（.sources、$$书源/.name 等）
```

说明（结合代码与实测备份）：

- 额外打包入口是 `components.t.w()`：遍历书架项路径 + 历史前 80 条路径，要求路径通过 `m31284`/`e.K5` 筛选且文件存在，再 `m8922` 写入 zip。
- **不要理解成“所有本地 txt 都会进备份”。** 样本 `ssds(3).backup` 中书架上的 `debug_log.txt`（本地书）**未**出现在 `_names.list`；出现的外部文件几乎都是 `.Books/**.wbpub` 及其伴随文件。
- 伴随元数据目录结构明显按 **`.wbpub` 网文书缓存** 设计，不是按任意用户 txt 设计。
- 若某个本地书路径恰好通过筛选且文件存在，代码仍可能打包该文件本体；但以当前样本与伴随文件逻辑看，**额外体积主要来自网文 `.wbpub` 缓存，而不是本地 txt 正文。**

## 本地备份文件位置

备份文件名：

```text
ssds.backup
```

完整路径：

```text
C0296.m29316() + "/ssds.backup"
```

`C0296.m29316()` 返回 `e.f8322u`。`e.f8322u` 的初始化逻辑在 `books.e.F4(...)` 中：

```text
e.f8322u = C0220.m16085() + "/Soushu"
```

`C0220.m16085()` 返回书库根目录 `e.f8315t`。初始化时优先选择外部存储根下的：

```text
/Books
```

失败时还会尝试：

```text
/mnt/sdcard/Books
/mnt/extsdcard/Books
```

结合隐私页明文说明，默认专用目录可理解为：

```text
/sdcard/Books/Soushu/ssds.backup
```

恢复时读取：

```text
/Books/Soushu/ssds.backup
```

如果不存在，则兼容旧文件名：

```text
/Books/Soushu/.sssq.backup
```

## 备份包格式

`ssds.backup` 本质上是普通 zip 包。

打包入口：

```java
components.t.j(String srcDir, String outFile, boolean z6, boolean z7)
```

本地备份调用参数：

```text
srcDir  = ApplicationInfo.dataDir
outFile = /Books/Soushu/ssds.backup
z6      = true
z7      = true
```

当 `z6=true` 时，zip 包中不直接使用真实路径作为 entry 名，而是把每个真实路径记录到 `_names.list`，文件内容 entry 使用序号形式：

```text
<备份根目录名>/1.tag
<备份根目录名>/2.tag
...
<备份根目录名>/_names.list
```

`_names.list` 中保存真实路径列表，每行一个原始相对路径或外部文件路径。恢复时再根据序号 `.tag` 映射回真实路径。

这更像路径混淆/路径映射，不是加密。zip 内容仍是普通压缩数据。

### 手动解压后为什么只有 `.tag` 和 `_names.list`

本地备份调用 `components.t.j(..., true, true)`，第三个参数为 `true`，因此启用了 entry 名映射。启用后，zip 里不会保存原始文件名，而是统一保存成：

```text
1.tag
2.tag
3.tag
...
_names.list
```

`_names.list` 只有一个是正常的，它是全局路径清单。清单第 1 行对应 `1.tag`，第 2 行对应 `2.tag`，依此类推。

`.tag` 也不是文本格式扩展名，只是占位后缀。每个 `.tag` 里面仍然是原始文件内容，所以有的能用文本打开，有的会乱码：

- 原文件是 XML/JSON/TXT 时，`.tag` 可能能直接阅读。
- 原文件是 SQLite 数据库时，文本打开会乱码，应使用 SQLite 工具查看。
- 原文件是图片、EPUB、压缩包、字体、封面等二进制文件时，文本打开也会乱码。
- 原文件是小说正文但编码不是当前文本编辑器使用的编码时，也可能显示乱码。

手动查看时应先读 `_names.list`，按行号判断某个 `.tag` 原本是什么文件，再用对应工具打开或按原扩展名重命名。

## 备份了哪些应用私有数据

本地备份会递归扫描整个 `ApplicationInfo.dataDir`，也就是应用私有数据目录。这个目录一般包含：

- `shared_prefs/`
  - 应用设置
  - 阅读设置
  - 书架显示配置
  - 默认书库路径
  - 设备相关选项
  - 用户登录/会员/开关类配置等
- `databases/`
  - 书架数据库
  - 书源数据库
  - 章节列表
  - 正文缓存
  - Cookie
  - TTS 配置等
- 其他应用私有持久化文件
  - 书源、阅读历史、搜索历史、净化/替换规则、封面或索引类持久化数据等，具体取决于实际运行后 dataDir 内生成的文件

代码中能看到的 GreenDAO DAO 包括：

- `BookShelfDao`
  - 书架条目
  - 当前章节、页码
  - 更新时间
  - 新章节数量
  - 分组
  - 自定义封面
  - 替换规则开关
  - 变量等
- `BookSourceDao`
  - 书源地址、名称、分组、类型
  - 搜索规则
  - 发现规则
  - 详情规则
  - 章节规则
  - 正文规则
  - UA、登录地址、启用状态、权重等
- `BookChapterDao`
  - 章节列表/章节信息
- `BookContentBeanDao`
  - 正文内容缓存
- `CookieBeanDao`
  - Cookie 数据
- `HttpTTSDao`
  - HTTP TTS 配置
- `CacheDao`
  - 缓存类表

因为备份逻辑是目录级递归，所以它不是按 DAO 单独导出，而是把 dataDir 中未被过滤的实际文件整体打包。

### 书架与书籍详情的归属关系在哪里

> 纠正说明：下面这段是早期基于别的样本/路径做的推断，不适用于当前 `com.flyersoft.seekbooks` 测试备份。  
> 这份样本里，真实的书架与书籍归属在 `com.flyersoft.seekbooks/databases/mrbooks.db` 的 `books.favorite`，`moon-db` 里的 `BOOK_SHELF` 是空的。

搜书大师的书架和书籍详情关系不在单独文件里，也不在 `_names.list` 里。`_names.list` 只负责把 `1.tag`、`2.tag` 这类文件名映射回真实路径。

真正的归属关系在备份包内某个 SQLite 数据库 `.tag` 文件中。定位方式是：

1. 按 `_names.list` 找到每个 `N.tag` 的真实路径。
2. 找到原路径属于 `databases/`，并且文件头是 SQLite 的 `.tag`。
3. 打开包含 `BOOK_SHELF`、`BOOK_INFO_BEAN`、`BOOK_CHAPTER`、`BOOK_CONTENT_BEAN` 等表的数据库。

关系字段是 `NOTE_URL`：

```text
BOOK_SHELF.NOTE_URL        书架项主键
BOOK_INFO_BEAN.NOTE_URL    书籍详情主键
BOOK_CHAPTER.NOTE_URL      章节归属到哪本书
BOOK_CONTENT_BEAN.NOTE_URL 正文缓存归属到哪本书
```

也就是：

```text
BOOK_SHELF.NOTE_URL = BOOK_INFO_BEAN.NOTE_URL
BOOK_CHAPTER.NOTE_URL = BOOK_SHELF.NOTE_URL
BOOK_CONTENT_BEAN.NOTE_URL = BOOK_SHELF.NOTE_URL
```

`BOOK_SHELF` 保存这本书在书架上的状态，例如阅读进度、当前章节、最近阅读时间、排序、分组、自定义封面等；`BOOK_INFO_BEAN` 保存书籍详情，例如书名、作者、简介、封面、目录 URL、来源名称、分类、字数等。两者不是在备份包里表现为“一个书架文件包含多个书籍文件”，而是在同一个 SQLite 数据库里通过 `NOTE_URL` 做一对一关联。

`TAG` 字段通常表示书源 URL 或本地标识，`BOOK_SHELF.TAG` 和 `BOOK_INFO_BEAN.TAG` 正常应一致，但不能把它当成书架归属主键；转换或分析时应以 `NOTE_URL` 为准。

## 明确不会备份/会过滤的数据

过滤函数在 `components.t.r(String path)`。

命中过滤规则的文件不会进入备份包。已解出的主要过滤项包括：

```text
/cache
_cache/
/google_
webview
/info.xml
/um
beta_values
/files/
/shaders/
/app_
/tx_
/oat/
/gdt
wxop_
_qq_
adsdk
profiles_
_dex
msp.db
/bugly
.dex
/lib/
/ttopen
.wj
.wj2
.sub
.so
settings.xml
```

另外对 `/alpha_*.png` 有特殊判断，通常也会过滤掉一批运行期生成图片，但会排除一个当前特殊文件。

整体看，过滤目标主要是：

- 缓存目录
- WebView 缓存
- dex/oat/lib/so 等运行时或安装产物
- 广告 SDK、Bugly、QQ/微信/支付相关运行数据
- 临时文件、缓存文件、部分设置文件

也就是说，备份侧有意保留“用户持久数据”，跳过大量运行时垃圾和第三方 SDK 产物。

## 额外备份的外部书路径文件（多为 `.wbpub`，不保证本地 txt）

本地备份默认调用 `doBackup3(true)`，因此 `components.t.j(..., z7=true)` 会触发：

```java
components.t.w(ZipOutputStream)
```

它会收集两类**外部书路径**（在应用私有目录之外、且通过筛选、文件仍存在）：

1. 书架中的书路径
   - 来源：`C0309.m30764() -> g.D()`
   - 每个书架项取 `g.e.f8513b`（书架记录上的文件路径）
   - 再经 `m31284`（内部到 `books.e.K5`）筛选 + `文件存在` 判断

2. 最近阅读/历史列表中的书路径
   - 来源：`C0311.m30938() -> e.K2()`
   - 最多取前 80 个
   - 会去重
   - 同样要求通过筛选且文件存在

对每个被纳入的路径，备份会尝试打包：

```text
该路径对应的文件本体
```

以及围绕该路径推导出的书籍伴随数据：

```text
<书籍所在目录>/.sources
<书籍所在目录>/<书籍ID>/.name
<书籍所在目录>/<书籍ID>/.author
<书籍所在目录>/<书籍ID>/.chapters
<书籍所在目录>/<书籍ID>/.latestc
<书籍所在目录>/<书籍ID>/.url
<书籍所在目录>/<书籍ID>/.description
<书籍所在目录>/<书籍ID>/.tag
<书籍所在目录>/<书籍ID>/.varible
<书籍所在目录>/<书籍ID>/.cover
```

这里的 `<书籍ID>` 不是直接用书名，而是通过 `books.t.H(bookPath)` 从原书文件内容派生出的标识值。`books.r.q0(bookPath)` 负责取原书文件所在目录。

如果 `.cover` 不存在，代码还会尝试从其他封面路径取图，生成宽度不超过 96px 的缩略图，再写成 `.cover` 后打包。

### 实测样本结论（重要）

对 `ssds(3).backup` / 解压样本：

| 书架书 | 路径类型 | 是否出现在备份 `_names.list` |
|--------|----------|------------------------------|
| `debug_log` | 本地 txt：`/sdcard/Books/Soushu/debug_log.txt` | **否** |
| 斗破苍穹 等 | 网文缓存：`/sdcard/Books/Soushu/.Books/.../*.wbpub` | **是**（含伴随文件） |

因此文档若写“默认会打包本地书原文件”，容易让人以为用户导入的 txt/epub 正文一定在备份里——**这与样本不符**。更准确的说法是：

> 额外打包逻辑会处理“通过筛选的外部书路径”；样本中实际打进去的主要是 **`.wbpub` 网文书缓存 + 伴随元数据**，**不能**默认本地 txt 已被打包。


## 本地恢复逻辑

本地恢复入口：

```text
AboutAct.doRestore()
  -> 本地恢复
  -> doRestore2(path, ...)
  -> doRestore3(path)
  -> doRestore4(path)
```

核心调用：

```java
C0162.m8545(
    backupFile,
    C0116.m3650(C0300.m29613()),
    false,
    callback,
    true,
    true,
    true
)
```

实际代理到：

```java
components.t.y(...)
```

恢复目标目录是当前应用 dataDir 的父/根拼接路径。恢复函数会读取 zip entry；如果 entry 是 `1.tag` 这种映射名，则通过 `_names.list` 找回原路径并写回。

恢复成功后的 callback 会继续做几件事：

1. 修改恢复出的 `shared_prefs/options1002.xml`
   - 将旧设备相关值适配到当前设备
   - 已确认的 key 包括：

```text
default_book_folder
isFoldablePhone
privacyOk
cutoutScreen3
autoCollectVer
fitCutout3
cacheNoAdTime
statusFontSize
statusMargin
shelfFontSize5
shelfCoverSize6
fileCoverSize5
```

2. 检查默认书库目录
   - 如果备份中的 `default_book_folder` 在当前设备不可用，则切到当前可用路径
   - 默认兜底路径可见为 `/sdcard/Books`

3. 清理旧的 shared_prefs/xml
   - `deleteUnusedShare_PrefFiles(arrayList)` 会遍历当前 dataDir 下文件
   - 对未出现在备份清单中的旧 XML/shared_prefs 文件进行清理
   - 但会跳过部分特殊文件

4. 发送恢复成功消息 `1003`
   - UI 提示恢复成功
   - 后续会要求重启/退出应用以使配置生效

失败时发送 `1004`。

## 云备份与本地备份的关系

虽然本次重点是本地备份，但云备份可以辅助理解：云端并没有另一套数据导出逻辑。

云备份流程：

```text
doCloudBackup2()
  -> C0230.m17846(dataDir, getCloudBackupFile(), true, true)
  -> 生成 cloud.backup
doCloudBackup3()
  -> 上传 cloud.backup
```

云恢复流程：

```text
查询云端用户文件列表
找到 cloud.backup
下载到本地 getCloudBackupFile()
调用同一个 doRestore4(cloud.backup)
```

因此云端只是传输同样结构的备份包。

## 本地备份数据范围总结

本地备份包含：

- 应用私有目录中未被过滤的持久化文件
  - shared_prefs 配置
  - 数据库
  - 书架数据
  - 书源数据
  - 阅读进度
  - 章节列表
  - 正文缓存
  - Cookie
  - TTS 配置
  - 搜索/历史/规则等实际落在 dataDir 的持久数据
- 书架/历史中通过筛选的外部书路径文件（样本中主要是 `.wbpub` 网文缓存，**不保证**本地 txt）
- 上述路径的伴随元数据（`.sources`、`$$书源/.*` 等）
  - 书源
  - 书名
  - 作者
  - 章节列表
  - 最新章节
  - 原始 URL
  - 简介
  - 标签
  - 变量
  - 封面

本地备份不包含或一般会过滤：

- cache/webview 缓存
- dex/oat/lib/so 运行产物
- 广告 SDK/第三方 SDK 运行数据
- Bugly 等崩溃上报运行数据
- 临时文件和部分无关设置文件

## 审计注意点

1. 备份包是普通 zip，扩展名是 `.backup`。
2. zip 内 entry 名被映射成 `序号.tag`，真实路径在 `_names.list`，这不是强加密。
3. 本地备份在 `z7=true` 时可能带上外部书路径文件；样本中主要是 `.wbpub` 缓存，体积可能明显变大，但**不要**假定本地 txt 正文一定在包内。
4. 被额外打包的外部路径会进入 `_names.list`，恢复时可能尝试写回原路径（样本多为 `.Books/**.wbpub`）。
5. 恢复会覆盖当前应用数据，并清理部分未在备份中的旧 shared_prefs/xml，属于破坏性恢复。
6. 恢复后会修改部分设备相关配置，避免旧设备分辨率、刘海屏、书库路径等配置直接污染当前设备。
