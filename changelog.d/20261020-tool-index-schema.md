- **技能 schema 多了一个"索引档"：描述从常驻改成按需取回**（⚠️ **agent 对外行为零变更**，
  `index` **出厂关**，生产路径逐字节不变）。两件：
  ① **起因是一份内联的目录**：planner 单次输入 52,145 tok 里 tools schema 占 38,738（74%），
  而 schema 里 55.2% 是描述散文——函数级 21,783 字符是"这个本领管什么"（一份目录），
  参数级 23,766 是"每一格填什么"（签名的一部分）。这一笔只把**函数级**那一半压成一行索引
  （实测 82,520 → 64,450 字符，**planner 输入 −16.3%**），并给模型一个 `describe_skill(name)`
  把原文取回来。**结构与散文的边界**：函数名 / type / enum / required / 嵌套 schema
  一个字符不动（`enum` 是带参清单的拦截点）。**不删、只外置**——`eval/contract_position_ab.py`
  实测那句话**位置无关但本身承重**（住正文 84/84、住 schema 84/84、**整句删掉 73/84**）。
  **参数级那一半不在这一笔的射程里**（它是另一个杠杆，见 `docs/prefix-cache-audit-20261020.md` §4②）。
  ② **判据**：新增 `tests/test_tool_index.py` 五组——缺省档不含取回口且描述就是原文 /
  索引档剥掉 description 后与缺省档**逐字节相同** / `describe_skill` 的返回**逐字等于**
  索引化前那一格 / 索引行恒为原文前缀且非空互异 / 体积棘轮（索引 ≤ 描述原文的 25%）。
  设计与分阶段见 `docs/adr/adr-0014-planner-becomes-router-schema-on-demand.md`。
