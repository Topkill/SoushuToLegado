# 搜书大师本地备份与恢复逻辑分析

> 基于反编译代码（约 23.11）与实测备份样本（`ssds(3).backup` 等）整理。  
> 混淆名（`C0xxx`）只在必要时出现；字符串资源多加密，**路径后缀、配置 key 等未完全解密的地方会标明「推断」**，不以猜测名单充当事实。

---

## 1. 结论（先看这个）

| 点 | 结论 |
|----|------|
| 是不是系统备份？ | **否**。`AndroidManifest` 中 `android:allowBackup="false"`，应用自己做备份/恢复。 |
| 备份包是什么？ | **普通 zip**，扩展名 `.backup`。 |
| 包内怎么组织？ | 内容文件多映射为 `序号.tag`，真实路径写在 `_names.list`（第 n 行对应 `n.tag`）。不是强加密。 |
| 默认备份什么？ | **应用私有目录（dataDir）**里通过过滤器的文件 +（默认开启时）部分**外部书路径**。 |
| 外部书路径打什么？ | 实测主要是 `…/Books/Soushu/.Books/**.wbpub`（网文书缓存）及 `.sources`、`$$书源名/.name` 等伴随文件。 |
| 本地 txt 会进包吗？ | **不保证，样本中没有。** 书架上的 `debug_log.txt` 未出现在 `_names.list`。 |
| `z7` 废弃了吗？ | **没有。** 本地/云备份都会 `true`，会走额外外部路径打包。 |
| 云备份？ | 与本地**同一套打包**，只是再上传/下载；恢复同一套逻辑。 |

**一句话：**  
本地备份 = 打应用数据目录的 zip + 尽量带上网文 `.wbpub` 缓存；**不是**「保证把用户导入的本地 txt 正文一起打包」。

---

## 2. 入口与调用关系

界面在「关于 / 用户信息」相关页（`AboutAct`）。

### 2.1 备份

```text
doBackup()
  → 弹窗：本地备份 / 云端备份
本地：
  doBackup2()          // 先处理旧备份文件（重命名备份，后缀来自字符串表，常见为 *.old 一类）
  → doBackup3()
  → doBackup3(true)    // 参数 true = 开启「额外外部路径打包」
  → 后台线程：
       pack(dataDir, backupFile, writeNamesList=true, packExternalBooks=true)
  → 成功/失败通过 Handler 消息通知 UI
```

对应实现要点：

- `doBackup3()` 固定调用 `doBackup3(true)`。
- 真正打包：`components.t.j(srcDir, outFile, z6, z7)`  
  - `z6=true`：启用路径映射，最后写 `_names.list`  
  - `z7=true`：调用 `m8927` → `w()`，额外尝试打包外部书路径  

云备份同样 `pack(..., true, true)`，再上传生成的 `cloud.backup` 一类文件。

### 2.2 恢复

```text
doRestore()
  → 弹窗：本地恢复 / 云恢复
本地：
  选路径（getRestoreFile 等）
  → doRestore2 / doRestore3 / doRestore4(backupPath)
  → 解压 zip，按 _names.list 把映射名写回真实路径（多写回当前 dataDir 等）
  → 成功后适配部分 shared_prefs、清理部分多余 prefs 文件
  → UI 提示并通常需要重启生效
```

云恢复：下载云端同结构备份文件 → **同一个** `doRestore4`。

---

## 3. 备份文件落在哪

代码里：

- `getBackupFile()` = 书库根目录字符串 + 文件名（文件名来自加密字符串表）
- `getCloudBackupFile()` = 同根目录 + 另一文件名  
- `getRestoreFile()` = 优先一个路径，不存在再试第二个（兼容旧备份名）

结合产品说明与样本，**通常**可理解为：

```text
…/Books/Soushu/ssds.backup          // 本地备份（推断文件名）
…/Books/Soushu/cloud.backup         // 云备份本地缓存（推断）
```

书库根优先外部存储下的 `Books`，失败时可能再试 `mnt/sdcard/Books` 等（初始化逻辑在书库路径相关代码中）。  
**精确文件名字符串未在本文解密还原**，以设备上实际文件与 `_names.list` 为准。

---

## 4. 备份包格式（已用样本验证）

以 `ssds(3).backup` 为例：

```text
zip 内：
  com.flyersoft.seekbooks/1.tag
  com.flyersoft.seekbooks/2.tag
  …
  com.flyersoft.seekbooks/_names.list
```

- `_names.list`：每行一个**真实路径**（UTF-8 文本）  
- 第 `i` 行 ↔ zip 里 `i.tag` 的内容  
- 路径两类：  
  1. 应用内：`com.flyersoft.seekbooks/...`（相对 dataDir 的逻辑路径）  
  2. 外部：`/sdcard/Books/Soushu/.Books/...`  

样本统计（约）：

| 类别 | 数量级 |
|------|--------|
| 应用内路径 | ~27 |
| 外部路径 | ~54 |
| 外部里的 `.wbpub` | 5（与网文书数量一致） |
| 外部里的用户 `.txt` 正文 | **0** |

---

## 5. 打包算法（`components.t.j`）

```text
t.j(dataDir, outFile, z6, z7):
  1) 若 z6：初始化路径映射列表
  2) 递归打包 dataDir（经过滤器 r(path)）
  3) 若 z7：w(zip)  // 外部书路径
  4) 若 z6：写入 _names.list
  5) 关闭 zip
```

### 5.1 应用私有目录

- 源：`ApplicationInfo.dataDir`（如 `/data/user/0/com.flyersoft.seekbooks`）  
- 行为：目录级递归，**不是**按表导出 JSON  
- 过滤器 `r(path)`：一长串路径关键字黑名单（字符串加密）；跳过大量缓存/SDK 垃圾  
- **注意：** 过滤器并非「只留业务文件」。样本里仍可能出现 `lib-main/dso_*` 等，说明有些非业务文件也会进包。

应用内常见、对转换有用的内容（样本中存在/高概率存在）：

- `shared_prefs/*`（含 `history.txt`、`web_book_search`、`shelf_names.txt`、`options1002.xml` 等）  
- `databases/mrbooks.db`（书架等）  
- 其它 prefs / 库文件  

是否包含「章节缓存、正文缓存、Cookie…」取决于**是否落在 dataDir 且未被 r() 滤掉**，文档不写成「必定包含」。

### 5.2 外部书路径（`z7=true` → `w()`）

**仍在使用，不是死代码。**

```text
w(zip):
  paths = []
  for 书架每一项:
      path = 书架项文件路径
      if K5(path) 且 文件存在: paths.add(path)
  for 最近阅读/历史 前 80 条:
      if K5(path) 且 未重复 且 文件存在: paths.add(path)
  for path in paths:
      打包 path 本身
      打包 path 旁推导出的伴随文件
```

筛选：

```text
m31284(path) → books.e.K5(path)
K5：判断路径是否以某固定后缀结尾（解密字符串长度 6，与 ".wbpub" 长度一致）
```

因此：

- **会稳定覆盖的是以该后缀结尾的路径** → 实测即 `.wbpub`  
- **用户导入的本地 txt（如 `…/debug_log.txt`）过不了 K5** → 样本中未打包  
- 不要写成「打包所有本地书」或「打包所有书架文件」

伴随文件（按代码会尝试；样本中可见）：

```text
<书目录>/.sources
<书目录>/<书源目录>/.name
<书目录>/<书源目录>/.author
<书目录>/<书源目录>/.chapters
<书目录>/<书源目录>/.latestc
<书目录>/<书源目录>/.url
<书目录>/<书源目录>/.description
<书目录>/<书源目录>/.tag
<书目录>/<书源目录>/.varible   // 源码拼写
<书目录>/<书源目录>/.cover
```

样本中 `<书源目录>` 形如 `$$阅读助手`、`$$QQ阅读`，是**书源名目录**，不是「从正文内容哈希出的书籍 ID」。  
若 `.cover` 不存在，代码可能从其它封面路径生成缩略图再写入。

### 5.3 样本对照（书架 vs 是否进备份）

| 书名 | 路径类型 | 是否在 `_names.list` |
|------|----------|----------------------|
| debug_log | 本地 txt：`/sdcard/Books/Soushu/debug_log.txt` | **否** |
| 斗破苍穹 等 5 本 | `.Books/.../*.wbpub` | **是**（含伴随文件） |

---

## 6. 恢复算法（能确认的部分）

```text
doRestore4(backupFile):
  解压 zip
  映射名通过 _names.list 还原为真实路径并写出
  成功后：
    - 调整部分 shared_prefs（设备相关，避免旧机配置硬套新机）
    - 检查默认书库目录是否可用
    - 清理当前 dataDir 中「未出现在备份清单」的部分 shared_prefs/xml（有白名单跳过）
    - 通知 UI 成功；通常需重启
  失败：通知 UI 失败
```

说明：

- 外部路径（如 `/sdcard/Books/Soushu/.Books/...`）恢复时可能**写回原绝对路径**；新机路径不同可能导致网文缓存/本地路径失效。  
- 恢复会覆盖当前应用数据，属于**破坏性操作**，应先备份。  
- 具体改哪些 options key、消息号（1001/1002/…）在反编译里存在，但字符串/常量未全部钉死，**此处不列完整 key 表**，避免假精确。

---

## 7. 云备份与本地备份

| | 本地 | 云 |
|--|------|-----|
| 打包 | 同一 `t.j(..., true, true)` | 同左 |
| 产物 | `ssds.backup`（推断名） | 先本地 `cloud.backup` 再上传 |
| 恢复 | `doRestore4(本地文件)` | 下载后 `doRestore4(同一结构)` |

云端**没有**第二套数据模型，只是传输通道。

---

## 8. 备份内容范围（按可信度）

### 8.1 高置信（代码 + 样本）

- dataDir 内经 `r()` 后的持久文件（prefs、sqlite 等）  
- `_names.list` + 序号映射  
- `z7=true` 时额外的 **`.wbpub` + 伴随元数据**（样本）  

### 8.2 中等置信

- 默认落在 `…/Books/Soushu/`  
- 历史路径最多 80 条参与外部打包扫描  
- 恢复后做设备相关 prefs 适配与多余 prefs 清理  

### 8.3 不保证 / 常见误解

| 误解 | 实际 |
|------|------|
| 本地 txt 正文一定在备份里 | **否**；样本未打包 |
| 备份 = 只备份配置 | **否**；还有 db、外部 `.wbpub` 等 |
| 备份 = 完整可离线搬家所有书 | **否**；本地 txt 可能只有库记录、无正文 |
| 过滤器后包内绝对干净 | **否**；仍可能有 lib-main 等 |
| `z7` 已废弃 | **否**；默认 true |

---

## 9. 和本仓库转换脚本的关系

转换脚本 `convert_bookshelf_backup.py`：

- 从备份中的 **`mrbooks.db` + prefs +（若有）`.wbpub` 伴随文件** 读书架与进度  
- **不依赖**本地 txt 是否在备份包内  
- 本地书若只有路径、没有文件，转成 Legado 后同样可能打不开正文  

合并脚本只处理已转换的 json，不改变搜书大师备份打包行为。

---

## 10. 审计与二次分析注意点

1. 备份是 zip，可直接解压 / 用 zip 库读。  
2. 业务还原优先：`_names.list` → 定位 `mrbooks.db`、prefs、外部 `.wbpub`。  
3. 判断「某本书有没有正文/缓存」：看路径是否在 `_names.list`，不要只看书架表。  
4. 混淆方法名、加密字符串会变版本；**以行为 + 样本为准**，少写死中间 wrapper 名。  
5. 恢复会写外部绝对路径：换机后 `.wbpub` / 书库目录可能失效，需用户重下或改路径。

---

## 11. 关键代码位置（反编译）

| 位置 | 作用 |
|------|------|
| `sources/com/flyersoft/WB/AboutAct.java` | `doBackup*` / `doRestore*` / `getBackupFile` / `getRestoreFile` |
| `sources/com/flyersoft/components/t.java` | `j` 打包入口；`w` 外部路径；`r` 目录过滤；`_names.list` 写出 |
| `sources/com/flyersoft/books/e.java` | `K5(path)` 外部路径后缀筛选 |
| `resources/AndroidManifest.xml` | `allowBackup="false"` |

---

## 12. 修订说明（相对旧文档）

旧文档主要问题：

1. 把额外打包写成「本地书/本地 txt 原文件」→ **与样本和 K5 筛选不符**。  
2. 过滤器/恢复 key 名单在未解密情况下写得过死。  
3. 伴随目录写成「内容派生书籍 ID」→ 样本实为 **`$$书源名` 目录**。  
4. 混淆调用链过长，可读性差、难维护。  

本文改为：**机制（代码）+ 实测（样本）+ 明确「不保证」项**，避免假精确。
