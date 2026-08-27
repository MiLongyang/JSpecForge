-- MySQL dump 10.13  Distrib 8.0.43, for Win64 (x86_64)
--
-- Host: localhost    Database: jspecforge
-- ------------------------------------------------------
-- Server version	8.0.43

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

--
-- Table structure for table `oj_attempts`
--

DROP TABLE IF EXISTS `oj_attempts`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `oj_attempts` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '自增ID',
  `task_id` int NOT NULL COMMENT '关联的任务ID',
  `a_round` int NOT NULL COMMENT '对应 Task A 的轮次（0-based）',
  `status` varchar(20) NOT NULL COMMENT 'pass / failed_syntax / failed_logic / timeout / unsupported_jml / skipped_blacklist',
  `exit_code` int DEFAULT NULL COMMENT 'OpenJML 閫?嚭鐮併?skipped_blacklist 鏃朵负 NULL銆傛敞鎰忥細OJ 涓嶄細鑷?劧杩斿洖 5',
  `elapsed_seconds` decimal(10,2) DEFAULT NULL COMMENT 'OJ 执行耗时（秒）。skipped_blacklist 时为 NULL',
  `detail_truncated` text COMMENT '截取的报错信息',
  `detail_full_length` int DEFAULT NULL COMMENT '完整报错字符长度',
  `full_java_with_jml` text COMMENT 'OJ ESC pass 时的完整 Java+JML 源码（仅 status=pass 时填写）',
  `mutation_score` decimal(6,4) DEFAULT NULL COMMENT 'MT 变异分数（内联 MT 跑完后回填，-1 表示 Major 失败）',
  `action_taken` varchar(255) NOT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `mt_killed` int DEFAULT NULL COMMENT '变异体被杀数',
  `mt_survived` int DEFAULT NULL COMMENT '变异体存活数',
  `mt_total` int DEFAULT NULL COMMENT '变异体总数',
  `mt_feedback_text` text COMMENT 'MT 存活变异体摘要',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_task_round` (`task_id`,`a_round`),
  CONSTRAINT `fk_oj_task` FOREIGN KEY (`task_id`) REFERENCES `tasks` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=19040 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='OJ ESC 检测记录表，每轮 Task A 一条';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `oj_bug_reports`
--

DROP TABLE IF EXISTS `oj_bug_reports`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `oj_bug_reports` (
  `id` int NOT NULL AUTO_INCREMENT,
  `error_pattern` varchar(255) NOT NULL,
  `task_id` int NOT NULL,
  `a_round` int NOT NULL,
  `jml_source` text,
  `method_code` text,
  `rac_detail` text,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_error_pattern` (`error_pattern`),
  KEY `idx_task` (`task_id`),
  CONSTRAINT `fk_oj_bug_task` FOREIGN KEY (`task_id`) REFERENCES `tasks` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=6585 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `pbt_attempts`
--

DROP TABLE IF EXISTS `pbt_attempts`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `pbt_attempts` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '自增ID',
  `task_id` int NOT NULL COMMENT '关联的任务ID',
  `a_round` int NOT NULL COMMENT '对应 Task A 的轮次（0-based）',
  `ig_round` int NOT NULL COMMENT '对应 Input Gen 的轮次（0-based）',
  `full_java_with_jml` text NOT NULL COMMENT '本次测试的 JML 注释完整 Java 源码',
  `input_generator_code` text NOT NULL COMMENT '本次使用的输入生成器代码',
  `has_blacklist_syntax` tinyint(1) NOT NULL DEFAULT '0' COMMENT '本轮 JML 是否包含黑名单语法',
  `blacklist_hits` json DEFAULT NULL COMMENT '命中的黑名单关键字列表 JSON',
  `is_pass` tinyint(1) NOT NULL COMMENT '本次 PBT 是否 100% 通过',
  `total_runs` int NOT NULL COMMENT '总测试次数',
  `gen_error_count` int NOT NULL DEFAULT '0' COMMENT '输入生成器异常次数',
  `precondition_reject_count` int NOT NULL DEFAULT '0' COMMENT '前置条件拒绝次数',
  `post_fail_count` int NOT NULL DEFAULT '0' COMMENT 'postcondition 失败次数',
  `seed` int NOT NULL COMMENT 'PBT 使用的随机种子',
  `elapsed_seconds` decimal(10,2) NOT NULL COMMENT '动态测试执行耗时（秒）',
  `failure_samples` json DEFAULT NULL COMMENT '按 JML 子句分类去重的失败样本 JSON',
  `detail_truncated` text,
  `detail_full_length` int NOT NULL DEFAULT '0',
  `action_taken` varchar(255) NOT NULL,
  `mutation_score` decimal(6,4) DEFAULT NULL COMMENT 'MT 变异分数（主流水线不写）',
  `valid_input_ratio` float DEFAULT NULL COMMENT '有效输入占比 = 有效输入数 / 总输入数',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `mt_killed` int DEFAULT NULL COMMENT '变异体被杀数',
  `mt_survived` int DEFAULT NULL COMMENT '变异体存活数',
  `mt_total` int DEFAULT NULL COMMENT '变异体总数',
  `mt_feedback_text` text COMMENT 'MT 存活变异体摘要',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_task_round_ig` (`task_id`,`a_round`,`ig_round`),
  KEY `idx_task` (`task_id`),
  KEY `idx_task_pass` (`task_id`,`is_pass`),
  CONSTRAINT `fk_pbt_task` FOREIGN KEY (`task_id`) REFERENCES `tasks` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=24456 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='PBT 动态测试记录表，存储所有尝试（通过+失败）';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `rac_attempts`
--

DROP TABLE IF EXISTS `rac_attempts`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `rac_attempts` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '自增ID',
  `task_id` int NOT NULL COMMENT '关联的任务ID',
  `a_round` int NOT NULL COMMENT '对应 Task A 的轮次（0-based）',
  `status` varchar(10) NOT NULL COMMENT 'pass / failed',
  `exit_code` int NOT NULL COMMENT 'RAC 退出码',
  `elapsed_seconds` decimal(10,2) NOT NULL COMMENT 'RAC 编译耗时（秒）',
  `detail_truncated` text COMMENT 'RAC 报错信息截取',
  `detail_full_length` int NOT NULL DEFAULT '0' COMMENT '完整报错字符长度',
  `action_taken` varchar(255) NOT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_task_round` (`task_id`,`a_round`),
  CONSTRAINT `fk_rac_task` FOREIGN KEY (`task_id`) REFERENCES `tasks` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=15135 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='RAC 编译记录表，仅进入 RP 路径时产生';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `task_llm_messages`
--

DROP TABLE IF EXISTS `task_llm_messages`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `task_llm_messages` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '消息自增ID',
  `task_id` int NOT NULL COMMENT '关联的任务ID',
  `session_type` enum('task_a','input_gen') NOT NULL COMMENT '会话类型',
  `a_round` int NOT NULL DEFAULT '0' COMMENT 'task_a：固定为 0；input_gen：由哪一轮 Task A 触发',
  `seq` int NOT NULL COMMENT '会话内全局顺序序号',
  `round` int NOT NULL DEFAULT '0' COMMENT '对话轮次。system=0, 第一轮 user+assistant=0, 第二轮=1, ...',
  `role` enum('system','user','assistant') NOT NULL COMMENT '消息角色',
  `content` longtext NOT NULL COMMENT '消息内容',
  `call_meta` json DEFAULT NULL COMMENT 'LLM 调用元数据 JSON（仅 assistant）',
  `is_oj_pass` tinyint(1) DEFAULT NULL COMMENT 'task_a assistant：OJ 是否通过',
  `is_pbt_pass` tinyint(1) DEFAULT NULL COMMENT 'task_a assistant：PBT 是否 100% 通过',
  `action_taken` varchar(255) DEFAULT NULL,
  `compile_status` varchar(10) DEFAULT NULL COMMENT 'input_gen assistant：编译状态 pass/failed',
  `compile_error` text COMMENT 'input_gen assistant：编译错误信息',
  `is_final_success` tinyint(1) DEFAULT NULL COMMENT 'input_gen assistant：输入生成器是否最终可用',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '消息创建时间',
  PRIMARY KEY (`id`),
  KEY `idx_task_session_seq` (`task_id`,`session_type`,`a_round`,`seq`),
  KEY `idx_task_role` (`task_id`,`role`,`session_type`),
  CONSTRAINT `fk_messages_task` FOREIGN KEY (`task_id`) REFERENCES `tasks` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=88705 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='LLM 对话消息表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `tasks`
--

DROP TABLE IF EXISTS `tasks`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `tasks` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '任务自增ID，机器内部使用，导出时由合并脚本重新分配',
  `source_dataset_name` varchar(100) NOT NULL COMMENT '来源数据集名称，如 Code2Bench、HumanEval',
  `source_dataset_index` int NOT NULL COMMENT '来源数据集内序号，与 source_dataset_name 组合构成全局唯一标识',
  `source_task_id` varchar(200) NOT NULL COMMENT '原始数据集中的 task_id 字段',
  `source_machine` varchar(100) NOT NULL COMMENT '来源机器标识，如 local / cloud_server_1',
  `nl_description` text NOT NULL COMMENT '自然语言需求描述',
  `method_code` text NOT NULL COMMENT '方法级 Java 源码',
  `full_code` text NOT NULL COMMENT '完整类 Java 源码',
  `input_generator_code` text COMMENT '任务级输入生成器代码',
  `branch_coverage` float DEFAULT NULL COMMENT '生成器分支覆盖率',
  `status` enum('pending','in_progress','success','failed') NOT NULL DEFAULT 'pending' COMMENT '任务状态',
  `current_step` varchar(30) DEFAULT NULL COMMENT '当前所处阶段：task_a / openjml / rac / input_gen / pbt / final',
  `a_round` int NOT NULL DEFAULT '0' COMMENT '当前 Task A 的轮次（最新 assistant 的 round 值）',
  `task_a_logic_retry` int NOT NULL DEFAULT '0' COMMENT '逻辑错误重试累计（OJ逻辑失败 + postcondition失败），不重置',
  `task_a_syntax_retry_consecutive` int NOT NULL DEFAULT '0' COMMENT '语法错误当前连续次数，生成语法正确 JML 后重置为 0',
  `task_a_syntax_retry_total` int NOT NULL DEFAULT '0' COMMENT '语法错误历史总计次数，只增不减',
  `pbt_pass_count` int NOT NULL DEFAULT '0' COMMENT 'PBT 100%通过的累计次数',
  `input_gen_retry_total` int NOT NULL DEFAULT '0' COMMENT '输入生成器重试累计总次数（跨所有 a_round），只增不减',
  `pbt_compile_retry_consecutive` int NOT NULL DEFAULT '0',
  `pbt_compile_retry_total` int NOT NULL DEFAULT '0',
  `task_a_insufficient_input_consecutive` int NOT NULL DEFAULT '0' COMMENT 'PBT 有效输入不足连续次数（P10 Phase 4）。PBT valid_ratio≥min 时重置为 0',
  `task_a_insufficient_input_total` int NOT NULL DEFAULT '0' COMMENT 'PBT 有效输入不足累计次数（P10 Phase 4），只增不减',
  `mt_oj_feedback_round` int NOT NULL DEFAULT '0' COMMENT 'OJ 路径 MT 反馈已用轮次',
  `mt_rp_feedback_round` int NOT NULL DEFAULT '0' COMMENT 'RP 路径 MT 反馈已用轮次',
  `path` varchar(20) DEFAULT NULL COMMENT '最终成功路径：openjml / rp（failed 时为 NULL）',
  `final_full_java_with_jml` text COMMENT '最终选定的 JML 注释完整 Java 源码',
  `final_a_round` int DEFAULT NULL COMMENT '最终答案来自 Task A 的第几轮（0-based）',
  `final_selected_candidate_index` int DEFAULT NULL COMMENT '仅 path=rp：从 pbt_attempts(is_pass=true) 中选中的索引',
  `failure_stage` varchar(30) DEFAULT NULL COMMENT '失败阶段：exhausted_syntax / exhausted_rounds',
  `failure_reason` varchar(500) DEFAULT NULL COMMENT '人类可读的失败原因描述',
  `last_error_detail` text COMMENT '最后一次失败的截取报错信息',
  `started_at` datetime DEFAULT NULL COMMENT '任务开始处理时间（首次 pending → in_progress）',
  `finished_at` datetime DEFAULT NULL COMMENT '任务完成时间（→ success 或 → failed）',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录最后更新时间',
  `oj_pass_count` int NOT NULL DEFAULT '0' COMMENT 'OJ ESC 通过候选累计数（P13）',
  `rp_disabled` tinyint(1) NOT NULL DEFAULT '0' COMMENT '是否已关闭 RP 通道（P13）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_source` (`source_dataset_name`,`source_dataset_index`),
  KEY `idx_status` (`status`),
  KEY `idx_status_step` (`status`,`current_step`)
) ENGINE=InnoDB AUTO_INCREMENT=3530 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='任务主表，每条原始数据集数据对应一行';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

-- Dump completed on 2026-06-14 18:14:13
