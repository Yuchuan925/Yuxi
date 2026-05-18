<template>
  <div class="knowledge-base-card">
    <!-- 标题栏 -->
    <div class="card-header">
      <div class="header-left">
        <a-button
          @click="backToDatabase"
          class="back-button"
          shape="circle"
          :icon="h(LeftOutlined)"
          type="text"
          size="small"
        ></a-button>
        <h3 class="card-title">{{ database.name || '数据库信息加载中' }}</h3>
      </div>
      <div class="header-right">
        <a-button type="text" size="small" @click="copyDatabaseId" title="复制知识库ID">
          <template #icon>
            <Copy :size="14" />
          </template>
        </a-button>
        <a-button @click="showEditModal" type="text" size="small">
          <template #icon>
            <Pencil :size="14" />
          </template>
        </a-button>
      </div>
    </div>

    <!-- 卡片内容 -->
    <div class="card-content">
      <!-- 描述文本 -->
      <div class="description">
        <p class="description-text">{{ database.description || '暂无描述' }}</p>
      </div>


    </div>
  </div>

  <!-- 编辑对话框 -->
  <a-modal v-model:open="editModalVisible" title="编辑知识库信息" width="700px">
    <template #footer>
      <a-button danger @click="deleteDatabase" style="margin-right: auto; margin-left: 0">
        <template #icon>
          <Trash2 :size="16" style="vertical-align: -3px; margin-right: 4px" />
        </template>
        删除数据库
      </a-button>
      <a-button key="back" @click="editModalVisible = false">取消</a-button>
      <a-button key="submit" type="primary" @click="handleEditSubmit">确定</a-button>
    </template>
    <a-form :model="editForm" :rules="rules" ref="editFormRef" layout="vertical">
      <a-form-item label="知识库名称" name="name" required>
        <a-input v-model:value="editForm.name" placeholder="请输入知识库名称" />
      </a-form-item>
      <a-form-item label="知识库描述" name="description">
        <AiTextarea
          v-model="editForm.description"
          :name="editForm.name"
          :files="fileList"
          placeholder="请输入知识库描述"
          :rows="4"
        />
      </a-form-item>

      <a-form-item
        v-if="!isReadOnlyConnector"
        label="自动生成问题"
        name="auto_generate_questions"
      >
        <a-switch
          v-model:checked="editForm.auto_generate_questions"
          checked-children="开启"
          un-checked-children="关闭"
        />
        <span style="margin-left: 8px; font-size: 12px; color: var(--gray-500)"
          >上传文件后自动生成测试问题</span
        >
      </a-form-item>

      <a-form-item v-if="!isReadOnlyConnector" name="chunk_preset_id">
        <template #label>
          <span class="chunk-preset-label">
            分块策略
            <a-tooltip :title="editPresetDescription">
              <QuestionCircleOutlined class="chunk-preset-help-icon" />
            </a-tooltip>
          </span>
        </template>
        <a-select v-model:value="editForm.chunk_preset_id" :options="chunkPresetOptions" />
      </a-form-item>

      <template v-if="isDifyKb">
        <a-form-item label="Dify API URL" name="dify_api_url">
          <a-input
            v-model:value="editForm.dify_api_url"
            placeholder="例如: https://api.dify.ai/v1"
          />
        </a-form-item>
        <a-form-item label="Dify Token" name="dify_token">
          <a-input-password
            v-model:value="editForm.dify_token"
            placeholder="请输入 Dify API Token"
          />
        </a-form-item>
        <a-form-item label="Dataset ID" name="dify_dataset_id">
          <a-input v-model:value="editForm.dify_dataset_id" placeholder="请输入 Dify dataset_id" />
        </a-form-item>
      </template>

      <template v-if="isNotionKb">
        <a-form-item label="Notion Token" name="notion_token">
          <a-input-password
            v-model:value="editForm.notion_token"
            placeholder="留空则保持现有 Token 或使用环境变量"
          />
        </a-form-item>
        <a-form-item label="Data Source ID" name="notion_data_source_id">
          <a-input
            v-model:value="editForm.notion_data_source_id"
            placeholder="请输入 Notion data_source_id"
          />
        </a-form-item>
        <a-form-item label="Notion API Version" name="notion_version">
          <a-input v-model:value="editForm.notion_version" placeholder="2026-03-11" />
        </a-form-item>
      </template>

      <!-- 共享配置（超级管理员可编辑，非共享时本部门管理员也可编辑） -->
      <a-form-item v-if="canEditShareConfig" label="共享设置" name="share_config">
        <a-form-item-rest>
          <ShareConfigForm
            ref="shareConfigFormRef"
            :model-value="database.share_config"
            :auto-select-user-dept="true"
          />
        </a-form-item-rest>
      </a-form-item>
      <!-- 非编辑状态下显示共享配置信息 -->
      <a-form-item v-else-if="database.share_config" label="共享设置" name="share_config_readonly">
        <div class="share-config-readonly">
          <a-tag :color="shareConfigDisplay.color">
            {{ shareConfigDisplay.label }}
          </a-tag>
          <span class="access-names">
            {{ shareConfigDisplay.detail }}
          </span>
        </div>
      </a-form-item>
    </a-form>
  </a-modal>
</template>

<script setup>
import { ref, reactive, computed, h, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useDatabaseStore } from '@/stores/database'
import { useUserStore } from '@/stores/user'
import { CHUNK_PRESET_OPTIONS, getChunkPresetDescription } from '@/utils/chunk_presets'
import { message } from 'ant-design-vue'
import { LeftOutlined, QuestionCircleOutlined } from '@ant-design/icons-vue'
import { Pencil, Trash2, Copy } from 'lucide-vue-next'
import { departmentApi } from '@/apis/department_api'
import { authApi } from '@/apis/auth_api'
import AiTextarea from '@/components/AiTextarea.vue'
import ShareConfigForm from '@/components/ShareConfigForm.vue'

const router = useRouter()
const store = useDatabaseStore()
const userStore = useUserStore()

const database = computed(() => store.database)
const isDifyKb = computed(() => database.value?.kb_type === 'dify')
const isNotionKb = computed(() => database.value?.kb_type === 'notion')
const isReadOnlyConnector = computed(() => isDifyKb.value || isNotionKb.value)

const departments = ref([])
const users = ref([])

const loadDepartments = async () => {
  try {
    const res = await departmentApi.getDepartments()
    departments.value = res.departments || res || []
  } catch (e) {
    console.error('加载部门列表失败:', e)
    departments.value = []
  }
}

const loadUsers = async () => {
  try {
    users.value = await authApi.getUserAccessOptions()
  } catch (e) {
    console.error('加载用户列表失败:', e)
    users.value = []
  }
}

onMounted(() => {
  loadDepartments()
  loadUsers()
})

const shareConfigDisplay = computed(() => {
  const shareConfig = database.value?.share_config || { access_level: 'global' }
  if (shareConfig.access_level === 'department') {
    const departmentIds = shareConfig.department_ids || []
    const names = departmentIds.map((id) => getDepartmentName(id)).join('、') || '无'
    return {
      color: 'blue',
      label: '部门共享',
      detail: `${departmentIds.length} 个部门可访问：${names}`
    }
  }

  if (shareConfig.access_level === 'user') {
    const userUids = shareConfig.user_uids || []
    const names = userUids.map((uid) => getUserName(uid)).join('、') || '无'
    return {
      color: 'purple',
      label: '指定人可访问',
      detail: `${userUids.length} 个用户可访问：${names}`
    }
  }

  return {
    color: 'green',
    label: '全局共享',
    detail: '所有用户可访问'
  }
})

const getDepartmentName = (id) => {
  const dept = departments.value.find((item) => Number(item.id) === Number(id))
  return dept?.name || `部门${id}`
}

const getUserName = (uid) => {
  const user = users.value.find((item) => item.uid === uid)
  return user?.username || uid
}

// 是否可以编辑共享配置
// 规则：1. 超级管理员可以编辑所有
//       2. 管理员也可以编辑（后端会验证权限）
const canEditShareConfig = computed(() => {
  if (userStore.isSuperAdmin) {
    return true
  }
  // 管理员可以编辑共享配置，后端会验证权限
  return userStore.isAdmin
})

const fileList = computed(() => {
  if (!database.value?.files) return []
  return Object.values(database.value.files)
    .map((f) => f.filename)
    .filter(Boolean)
})

// 复制数据库ID
const copyDatabaseId = async () => {
  if (!database.value.db_id) {
    message.warning('知识库ID为空')
    return
  }

  try {
    await navigator.clipboard.writeText(database.value.db_id)
    message.success('知识库ID已复制到剪贴板')
  } catch {
    // 降级方案
    const textArea = document.createElement('textarea')
    textArea.value = database.value.db_id
    document.body.appendChild(textArea)
    textArea.select()
    document.execCommand('copy')
    document.body.removeChild(textArea)
    message.success('知识库ID已复制到剪贴板')
  }
}

// 返回数据库列表
const backToDatabase = () => {
  router.push({ path: '/extensions', query: { tab: 'knowledge' } })
}

// 编辑相关逻辑（复用自 DatabaseHeader）
const editModalVisible = ref(false)
const editFormRef = ref(null)
const shareConfigFormRef = ref(null)
const editForm = reactive({
  name: '',
  description: '',
  auto_generate_questions: false,
  chunk_preset_id: 'general',
  dify_api_url: '',
  dify_token: '',
  dify_dataset_id: '',
  notion_token: '',
  notion_data_source_id: '',
  notion_version: '2026-03-11'
})

const chunkPresetOptions = CHUNK_PRESET_OPTIONS.map(({ label, value }) => ({ label, value }))
const editPresetDescription = computed(() => getChunkPresetDescription(editForm.chunk_preset_id))

const rules = {
  name: [{ required: true, message: '请输入知识库名称' }]
}

// 打开编辑弹窗
const showEditModal = () => {
  console.log('[showEditModal] 被调用')

  editForm.name = database.value.name || ''
  editForm.description = database.value.description || ''
  editForm.auto_generate_questions =
    database.value.additional_params?.auto_generate_questions || false
  editForm.chunk_preset_id = database.value.additional_params?.chunk_preset_id || 'general'
  editForm.dify_api_url = database.value.additional_params?.dify_api_url || ''
  editForm.dify_token = database.value.additional_params?.dify_token || ''
  editForm.dify_dataset_id = database.value.additional_params?.dify_dataset_id || ''
  editForm.notion_token = ''
  editForm.notion_data_source_id = database.value.additional_params?.notion_data_source_id || ''
  editForm.notion_version = database.value.additional_params?.notion_version || '2026-03-11'

  editModalVisible.value = true
}

const handleEditSubmit = () => {
  editFormRef.value
    .validate()
    .then(async () => {
      // 验证共享配置
      if (shareConfigFormRef.value) {
        const validation = shareConfigFormRef.value.validate()
        if (!validation.valid) {
          message.warning(validation.message)
          return
        }
      }

      const formConfig = shareConfigFormRef.value?.config || { access_level: 'global' }
      const updateData = {
        name: editForm.name,
        description: editForm.description,
        additional_params: {},
        share_config: {
          access_level: formConfig.access_level,
          department_ids: formConfig.access_level === 'department' ? formConfig.department_ids || [] : [],
          user_uids: formConfig.access_level === 'user' ? formConfig.user_uids || [] : []
        }
      }

      if (isDifyKb.value) {
        if (
          !editForm.dify_api_url?.trim() ||
          !editForm.dify_token?.trim() ||
          !editForm.dify_dataset_id?.trim()
        ) {
          message.error('请完整填写 Dify API URL、Token 和 Dataset ID')
          return
        }
        if (!editForm.dify_api_url.trim().endsWith('/v1')) {
          message.error('Dify API URL 必须以 /v1 结尾')
          return
        }
        updateData.additional_params = {
          dify_api_url: editForm.dify_api_url.trim(),
          dify_token: editForm.dify_token.trim(),
          dify_dataset_id: editForm.dify_dataset_id.trim()
        }
      } else if (isNotionKb.value) {
        if (!editForm.notion_data_source_id?.trim()) {
          message.error('请填写 Notion Data Source ID')
          return
        }
        updateData.additional_params = {
          notion_data_source_id: editForm.notion_data_source_id.trim(),
          notion_version: editForm.notion_version?.trim() || '2026-03-11'
        }
        if (editForm.notion_token?.trim()) {
          updateData.additional_params.notion_token = editForm.notion_token.trim()
        }
      } else {
        updateData.additional_params = {
          auto_generate_questions: editForm.auto_generate_questions,
          chunk_preset_id: editForm.chunk_preset_id || 'general'
        }
      }

      console.log(
        '[handleEditSubmit] updateData.share_config:',
        JSON.stringify(updateData.share_config)
      )

      await store.updateDatabaseInfo(updateData)
      editModalVisible.value = false
    })
    .catch((err) => {
      console.error('表单验证失败:', err)
    })
}

const deleteDatabase = () => {
  store.deleteDatabase()
}
</script>

<style lang="less" scoped>
.knowledge-base-card {
  background: linear-gradient(120deg, var(--main-30) 0%, var(--gray-0) 100%);
  border-radius: 12px;
  border: 1px solid var(--gray-200);
  margin-bottom: 8px;
}

// 只读共享配置显示
.share-config-readonly {
  display: flex;
  align-items: center;
  gap: 8px;

  .access-names {
    font-size: 13px;
    color: var(--gray-600);
  }
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;

  .header-left {
    display: flex;
    align-items: center;
    gap: 4px;
    flex: 1;
    min-width: 0;

    button.back-button {
      margin-left: -5px;
      font-size: 10px;
    }
  }

  .card-title {
    font-size: 16px;
    font-weight: 600;
    color: var(--gray-800);
    margin: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    // flex: 1;
  }

  .header-right {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-shrink: 0;

    button {
      color: var(--gray-500);
      height: 100%;
    }

    button:hover {
      color: var(--gray-700);
      background-color: var(--gray-100);
    }
  }
}

.card-content {
  padding: 0 16px 16px 16px;
}

.description {
  margin-bottom: 12px;

  .description-text {
    font-size: 14px;
    color: var(--gray-700);
    line-height: 1.5;
    margin: 0;
  }
}

.chunk-preset-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.chunk-preset-help-icon {
  color: var(--gray-500);
  cursor: help;
  font-size: 14px;
}
</style>
