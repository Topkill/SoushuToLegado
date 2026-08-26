# 合并到现有 Legado 备份

## 这是干嘛的？

转换脚本 `convert_bookshelf_backup.py` 会生成一个**只有书架相关文件**的小备份。  
如果你在 Legado 里**已经有书、书源、主题**，直接恢复这个小备份，往往会把现有数据盖掉，或者只剩转换过来的那一点。

**合并脚本**就是解决这个场景的：

> 以你现在的 Legado 备份为底，把搜书大师转出来的书架、分组、搜索历史、阅读时长**加进去**，尽量不毁掉原来的东西。

---

## 什么时候用？

| 情况 | 用哪个 |
|------|--------|
| Legado 是空的 / 你就想用转换结果当新备份 | 只跑 **转换**，直接导入 `backup-converted.zip` |
| Legado 里已有书源、书、设置，还想并入搜书大师书架 | 先 **转换**，再跑 **合并** |
| 只想看转换结果长什么样 | 只跑转换，不必合并 |

一句话：

- **只有搜书大师 → Legado**：转换就够了  
- **已有 Legado + 还要搜书大师**：转换 + 合并  

---

## 最简用法

在项目根目录执行（先装好 Python 3.10+）：

```powershell
# 1）搜书大师备份 → 精简 Legado zip
python convert_bookshelf_backup.py "ssds.backup" -o backup-converted.zip

# 2）合进你现有的 Legado 备份（zip 或解压目录都行）
python merge/merge_legado_backup.py --existing "你的legado备份.zip" --import backup-converted.zip
```

第 2 步默认生成：

```text
legado-merged.zip
```

然后在 Legado 里用 **恢复备份** 导入 `legado-merged.zip`。

### 更短的合并命令（默认输出名）

```powershell
python merge/merge_legado_backup.py --existing legadobackup --import backup-converted.zip
```

只要两个参数：`--existing`（现有 Legado）和 `--import`（转换结果）。

---

## 合并时大概会怎样？

以 **现有 Legado 为准**，搜书大师内容往后面加：

1. **分组**  
   搜书大师的书架变成新的自定义分组，接在你原来的分组后面。  
   分组 ID 会重新分配，避免和旧分组撞车。

2. **书**  
   - 同名同作者：认为 Legado 里已有，**跳过导入**（避免恢复时互相顶掉）  
     去重分两类：「导入备份与现有备份重复」以 Legado 已有的为准；「导入备份内重复」是搜书大师里就有一样的书，重复组每组保留一本  
   - 其它书：追加，并挂到新分好的组上  
   - 新书的手动排序号会按 Legado 习惯重算（`最小 order - 1` 往下减）

3. **搜索历史**  
   已有的词不动；新词接在后面。

4. **阅读时长**  
   同名书时长累加；最近阅读时间仍用 Legado 原来的。

5. **其它文件**  
   你原来备份里的书源、主题、配置等**原样保留**，不会被搜书那份精简备份清掉。

合并结束后终端会打印统计，加了多少组/书、跳过了哪些重名书。

存在去重书籍时，输出结束后还会追问一次：

- 输入 **1 或回车**：结束不查看
- 输入 **2**：在终端列出全部去重明细（按「导入备份与现有备份重复」「导入备份内重复」分组）
- 输入 **3**：导出到 `<输出文件名>-重复书籍.txt`

---

## 这个文件夹里有什么？

| 文件 | 作用 |
|------|------|
| `merge_legado_backup.py` | **总入口**，一般只用这个 |
| `merge_book_group.py` | 合并分组（总脚本会调用） |
| `merge_bookshelf.py` | 合并书架 |
| `merge_search_history.py` | 合并搜索历史 |
| `merge_read_record.py` | 合并阅读记录 |

一般不用单独跑后面四个；出问题排查时可以拆开用。

---

## 常用可选参数

```powershell
python merge/merge_legado_backup.py `
  --existing "legado备份.zip" `
  --import backup-converted.zip `
  -o my-merged.zip `
  --group-name-conflict number `
  --report merge-report.md `
  --verbose
```

| 参数 | 说明 |
|------|------|
| `-o` | 输出 zip，默认 `legado-merged.zip` |
| `--group-name-conflict keep` | 分组重名时仍用原名（默认） |
| `--group-name-conflict number` | 分组重名时改成 `名字（1）` |
| `--report` | 写一份人话报告 |
| `--verbose` | 多打一点细节 |

---

## 使用前请记住

1. **两边都先备份**：搜书大师 + 你现在的 Legado。  
2. 合并解决的是「备份 zip 怎么并」，**不能**把搜书大师书源变成 Legado 书源。  
3. 网文书导入后多半要**重新搜书/换源**才能继续看。  
4. 本地书只迁了书架信息，**正文文件**还得你自己放到手机上。  
5. 同名同作者的书会跳过搜书那本，以 Legado 里已有的为准；终端里会列出跳过了哪些。

---

## 和「只转换」的区别

```text
只转换：
  搜书备份 → backup-converted.zip → 直接恢复
  适合：新装 Legado / 不在乎覆盖现有数据

转换 + 合并：
  搜书备份 → backup-converted.zip
           ↘
  现有 Legado 备份 → 合并 → legado-merged.zip → 恢复
  适合：Legado 里已经有东西了，还想并入搜书大师书架
```

有现成 Legado 数据时，优先用合并，别直接拿转换结果去恢复。
