const { createApp, ref, reactive, computed, onMounted, watch } = Vue;

createApp({
  setup() {
    const recentBatches = ref([]);
    const currentBatch = ref(null);
    const matchResults = ref([]);
    const exceptions = ref([]);
    const toleranceHistory = ref([]);
    const auditLogs = ref([]);
    const batchSummary = ref(null);
    const activeTab = ref("upload");
    const showCreateModal = ref(false);
    const toast = reactive({ show: false, message: "", type: "success" });

    const createForm = reactive({ name: "", tolerance_pct: 2.0, tolerance_abs: 100.0 });
    const toleranceForm = reactive({ pct: 2.0, abs: 100.0 });

    const tabs = [
      { key: "upload", label: "文件上传" },
      { key: "results", label: "匹配结果" },
      { key: "exceptions", label: "异常待确认" },
      { key: "actions", label: "操作" },
      { key: "history", label: "历史记录" },
    ];

    const canUpload = computed(() => currentBatch.value && ["CREATED", "FAILED"].includes(currentBatch.value.status));
    const canMatch = computed(() => canUpload.value && currentBatch.value.po_filename && currentBatch.value.invoice_filename);
    const canConfirm = computed(() => currentBatch.value && ["MATCHED", "EXCEPTION_PENDING"].includes(currentBatch.value.status));
    const canPost = computed(() => currentBatch.value && currentBatch.value.status === "CONFIRMED");
    const canRollback = computed(() => currentBatch.value && currentBatch.value.status === "POSTED");
    const canReset = computed(() => currentBatch.value && ["ROLLED_BACK", "FAILED"].includes(currentBatch.value.status));

    function showToast(msg, type = "success") {
      toast.message = msg;
      toast.type = type;
      toast.show = true;
      setTimeout(() => { toast.show = false; }, 3000);
    }

    async function api(url, options = {}) {
      try {
        const res = await fetch(url, {
          headers: { "Content-Type": "application/json", ...options.headers },
          ...options,
        });
        const data = await res.json();
        if (!res.ok) {
          const detail = data.details ? data.details.join("; ") : data.error || "操作失败";
          showToast(detail, "error");
          return null;
        }
        return data;
      } catch (e) {
        showToast("网络错误: " + e.message, "error");
        return null;
      }
    }

    async function loadDashboard() {
      const data = await api("/api/dashboard");
      if (data) recentBatches.value = data.batches;
    }

    async function openBatch(id) {
      const data = await api(`/api/batches/${id}`);
      if (!data) return;
      currentBatch.value = data;
      batchSummary.value = data.summary || null;
      toleranceHistory.value = data.tolerance_history || [];
      auditLogs.value = data.audit_logs || [];
      toleranceForm.pct = data.tolerance_pct;
      toleranceForm.abs = data.tolerance_abs;
      activeTab.value = "upload";
      matchResults.value = [];
      exceptions.value = [];
    }

    async function createBatch() {
      if (!createForm.name.trim()) { showToast("请输入批次名称", "error"); return; }
      const data = await api("/api/batches", {
        method: "POST",
        body: JSON.stringify(createForm),
      });
      if (data) {
        showCreateModal.value = false;
        createForm.name = "";
        showToast("批次创建成功");
        await loadDashboard();
        await openBatch(data.id);
      }
    }

    async function uploadFile(event, type) {
      const file = event.target.files[0];
      if (!file) return;
      const formData = new FormData();
      formData.append("file", file);
      const url = type === "po" ? `/api/batches/${currentBatch.value.id}/upload-po` : `/api/batches/${currentBatch.value.id}/upload-invoice`;
      try {
        const res = await fetch(url, { method: "POST", body: formData });
        const data = await res.json();
        if (!res.ok) {
          const detail = data.details ? data.details.join("; ") : data.error || "上传失败";
          showToast(detail, "error");
          return;
        }
        showToast(data.message);
        await openBatch(currentBatch.value.id);
      } catch (e) {
        showToast("上传错误: " + e.message, "error");
      }
      event.target.value = "";
    }

    function handleDrop(event, type) {
      const file = event.dataTransfer.files[0];
      if (!file) return;
      const formData = new FormData();
      formData.append("file", file);
      const url = type === "po" ? `/api/batches/${currentBatch.value.id}/upload-po` : `/api/batches/${currentBatch.value.id}/upload-invoice`;
      fetch(url, { method: "POST", body: formData })
        .then(r => r.json())
        .then(data => {
          if (data.error) { showToast(data.details ? data.details.join("; ") : data.error, "error"); return; }
          showToast(data.message);
          openBatch(currentBatch.value.id);
        });
    }

    async function updateTolerance() {
      const data = await api(`/api/batches/${currentBatch.value.id}/tolerance`, {
        method: "PUT",
        body: JSON.stringify({ tolerance_pct: toleranceForm.pct, tolerance_abs: toleranceForm.abs }),
      });
      if (data) {
        showToast("容差配置已更新");
        await openBatch(data.id);
      }
    }

    async function runMatch() {
      const data = await api(`/api/batches/${currentBatch.value.id}/match`, { method: "POST" });
      if (data) {
        showToast(data.has_exceptions ? "匹配完成，存在异常需处理" : "匹配完成，全部匹配成功");
        await openBatch(currentBatch.value.id);
        if (data.has_exceptions) activeTab.value = "exceptions";
        else activeTab.value = "results";
      }
    }

    async function loadResults() {
      const data = await api(`/api/batches/${currentBatch.value.id}/results`);
      if (data) {
        matchResults.value = data.results;
        batchSummary.value = data.summary;
      }
    }

    async function loadExceptions() {
      const data = await api(`/api/batches/${currentBatch.value.id}/exceptions`);
      if (data) {
        exceptions.value = data.exceptions.map(e => ({ ...e, _remarks: e.remarks || "" }));
      }
    }

    async function saveRemark(exc) {
      const data = await api(`/api/batches/${currentBatch.value.id}/exceptions/${exc.id}/remark`, {
        method: "PUT",
        body: JSON.stringify({ remarks: exc._remarks }),
      });
      if (data) showToast("备注已保存");
    }

    async function resolveException(exc, action) {
      const data = await api(`/api/batches/${currentBatch.value.id}/exceptions/${exc.id}/resolve`, {
        method: "PUT",
        body: JSON.stringify({ action }),
      });
      if (data) {
        showToast(action === "resolve" ? "异常已确认" : "异常已驳回");
        await loadExceptions();
        await openBatch(currentBatch.value.id);
      }
    }

    async function confirmBatch() {
      const data = await api(`/api/batches/${currentBatch.value.id}/confirm`, { method: "POST" });
      if (data) { showToast("批次已确认"); await openBatch(data.id); }
    }

    async function postBatch() {
      const data = await api(`/api/batches/${currentBatch.value.id}/post`, { method: "POST" });
      if (data) { showToast("批次已入账"); await openBatch(data.id); }
    }

    async function rollbackBatch() {
      const data = await api(`/api/batches/${currentBatch.value.id}/rollback`, { method: "POST" });
      if (data) { showToast("批次已回滚"); await openBatch(data.id); }
    }

    async function resetBatch() {
      const data = await api(`/api/batches/${currentBatch.value.id}/reset`, { method: "POST" });
      if (data) { showToast("批次已重置"); await openBatch(data.id); }
    }

    function exportReport() {
      window.open(`/api/batches/${currentBatch.value.id}/export`, "_blank");
    }

    function statusLabel(s) {
      const m = { CREATED:"已创建", VALIDATING:"校验中", MATCHED:"已匹配", EXCEPTION_PENDING:"异常待确认", CONFIRMED:"已确认", POSTED:"已入账", ROLLED_BACK:"已回滚", FAILED:"失败" };
      return m[s] || s;
    }

    function statusClass(s) {
      const m = { CREATED:"bg-gray-100 text-gray-700", VALIDATING:"bg-blue-100 text-blue-700", MATCHED:"bg-green-100 text-green-700", EXCEPTION_PENDING:"bg-yellow-100 text-yellow-700", CONFIRMED:"bg-indigo-100 text-indigo-700", POSTED:"bg-purple-100 text-purple-700", ROLLED_BACK:"bg-orange-100 text-orange-700", FAILED:"bg-red-100 text-red-700" };
      return "status-badge " + (m[s] || "bg-gray-100 text-gray-700");
    }

    function matchTypeLabel(t) {
      const m = { EXACT:"精确匹配", TOLERANCE:"容差匹配", OVER_TOLERANCE:"超容差异常", UNMATCHED_PO:"未匹配采购单", UNMATCHED_INVOICE:"未匹配发票" };
      return m[t] || t;
    }

    function matchTypeClass(t) {
      const m = { EXACT:"bg-green-100 text-green-700 px-2 py-0.5 rounded text-xs", TOLERANCE:"bg-yellow-100 text-yellow-700 px-2 py-0.5 rounded text-xs", OVER_TOLERANCE:"bg-red-100 text-red-700 px-2 py-0.5 rounded text-xs", UNMATCHED_PO:"bg-red-100 text-red-700 px-2 py-0.5 rounded text-xs", UNMATCHED_INVOICE:"bg-orange-100 text-orange-700 px-2 py-0.5 rounded text-xs" };
      return m[t] || "";
    }

    function formatTime(iso) {
      if (!iso) return "";
      const d = new Date(iso);
      return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
    }

    watch(activeTab, (tab) => {
      if (!currentBatch.value) return;
      if (tab === "results") loadResults();
      else if (tab === "exceptions") loadExceptions();
    });

    onMounted(loadDashboard);

    return {
      recentBatches, currentBatch, matchResults, exceptions, toleranceHistory, auditLogs,
      batchSummary, activeTab, showCreateModal, toast, tabs,
      createForm, toleranceForm,
      canUpload, canMatch, canConfirm, canPost, canRollback, canReset,
      openBatch, createBatch, uploadFile, handleDrop, updateTolerance, runMatch,
      saveRemark, resolveException, confirmBatch, postBatch, rollbackBatch, resetBatch,
      exportReport, statusLabel, statusClass, matchTypeLabel, matchTypeClass, formatTime,
      showToast,
    };
  },
}).mount("#app");
