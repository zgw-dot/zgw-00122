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

    const recalcNotes = ref([]);
    const comparisonResult = ref(null);
    const comparisonHistory = ref([]);
    const comparisonFilter = ref("");
    const compareError = ref("");
    const compareForm = reactive({ noteAId: "", noteBId: "" });
    const reviewForm = reactive({ remark: "" });
    const selectedComparisonIds = ref(new Set());
    const batchReviewForm = reactive({ remark: "" });
    const batchConflictResult = ref(null);
    const poDraft = ref(null);
    const invoiceDraft = ref(null);

    const createForm = reactive({ name: "", tolerance_pct: 2.0, tolerance_abs: 100.0 });
    const toleranceForm = reactive({ pct: 2.0, abs: 100.0 });

    const tabs = [
      { key: "upload", label: "文件上传（预检模式）" },
      { key: "results", label: "匹配结果" },
      { key: "exceptions", label: "异常待确认" },
      { key: "recalc-notes", label: "重算说明" },
      { key: "actions", label: "操作" },
      { key: "history", label: "历史记录" },
    ];

    const canUpload = computed(() => currentBatch.value && ["CREATED", "FAILED"].includes(currentBatch.value.status));
    const canMatch = computed(() => canUpload.value && currentBatch.value.po_filename && currentBatch.value.invoice_filename);
    const canConfirm = computed(() => currentBatch.value && ["MATCHED", "EXCEPTION_PENDING"].includes(currentBatch.value.status));
    const canPost = computed(() => currentBatch.value && currentBatch.value.status === "CONFIRMED");
    const canRollback = computed(() => currentBatch.value && currentBatch.value.status === "POSTED");
    const canReset = computed(() => currentBatch.value && ["ROLLED_BACK", "FAILED"].includes(currentBatch.value.status));
    const canCompare = computed(() => compareForm.noteAId && compareForm.noteBId && compareForm.noteAId !== compareForm.noteBId);
    const hasSelectedComparisons = computed(() => selectedComparisonIds.value.size > 0);

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
      recalcNotes.value = [];
      comparisonResult.value = null;
      comparisonHistory.value = [];
      comparisonFilter.value = "";
      compareError.value = "";
      compareForm.noteAId = "";
      compareForm.noteBId = "";
      reviewForm.remark = "";
      poDraft.value = null;
      invoiceDraft.value = null;
      loadDrafts(id);
    }

    async function loadDrafts(batchId) {
      const poData = await api(`/api/batches/${batchId}/drafts/latest?file_type=PO`);
      if (poData && poData.draft && poData.draft.status === "PENDING") {
        poDraft.value = poData.draft;
      }
      const invData = await api(`/api/batches/${batchId}/drafts/latest?file_type=INVOICE`);
      if (invData && invData.draft && invData.draft.status === "PENDING") {
        invoiceDraft.value = invData.draft;
      }
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

    async function precheckFile(event, type) {
      const file = event.target.files[0];
      if (!file) return;
      const formData = new FormData();
      formData.append("file", file);
      formData.append("operator", "web_user");
      const url = type === "po" ? `/api/batches/${currentBatch.value.id}/precheck-po` : `/api/batches/${currentBatch.value.id}/precheck-invoice`;
      try {
        const res = await fetch(url, { method: "POST", body: formData });
        const data = await res.json();
        if (!res.ok) {
          const detail = data.details ? data.details.join("; ") : data.error || "预检失败";
          showToast(detail, "error");
          return;
        }
        if (type === "po") {
          poDraft.value = data;
        } else {
          invoiceDraft.value = data;
        }
        let msg = `预检完成: ${data.row_count} 行, ${data.error_count} 错误, ${data.warning_count} 警告`;
        if (data.conflict) {
          msg += ` (旧草稿 #${data.conflict.old_draft_id} 已自动丢弃)`;
        }
        if (!data.is_new) {
          msg = "文件内容未变化，使用已有草稿";
        }
        showToast(msg, data.error_count > 0 ? "error" : "success");
      } catch (e) {
        showToast("预检错误: " + e.message, "error");
      }
      event.target.value = "";
    }

    function handleDrop(event, type) {
      const file = event.dataTransfer.files[0];
      if (!file) return;
      const formData = new FormData();
      formData.append("file", file);
      formData.append("operator", "web_user");
      const url = type === "po" ? `/api/batches/${currentBatch.value.id}/precheck-po` : `/api/batches/${currentBatch.value.id}/precheck-invoice`;
      fetch(url, { method: "POST", body: formData })
        .then(r => r.json())
        .then(data => {
          if (data.error) { showToast(data.details ? data.details.join("; ") : data.error, "error"); return; }
          if (type === "po") {
            poDraft.value = data;
          } else {
            invoiceDraft.value = data;
          }
          let msg = `预检完成: ${data.row_count} 行, ${data.error_count} 错误, ${data.warning_count} 警告`;
          if (data.conflict) {
            msg += ` (旧草稿 #${data.conflict.old_draft_id} 已自动丢弃)`;
          }
          if (!data.is_new) {
            msg = "文件内容未变化，使用已有草稿";
          }
          showToast(msg, data.error_count > 0 ? "error" : "success");
        });
    }

    async function confirmDraft(draftId) {
      try {
        const res = await fetch(`/api/batches/${currentBatch.value.id}/drafts/${draftId}/confirm`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ operator: "web_user" }),
        });
        const data = await res.json();
        if (!res.ok) {
          const detail = data.details ? data.details.join("; ") : data.error || "确认失败";
          showToast(detail, "error");
          return;
        }
        showToast(`导入成功，共 ${data.imported_count} 行`);
        poDraft.value = null;
        invoiceDraft.value = null;
        await openBatch(currentBatch.value.id);
      } catch (e) {
        showToast("确认错误: " + e.message, "error");
      }
    }

    async function discardDraft(draftId) {
      try {
        const res = await fetch(`/api/batches/${currentBatch.value.id}/drafts/${draftId}/discard`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ operator: "web_user" }),
        });
        const data = await res.json();
        if (!res.ok) {
          const detail = data.details ? data.details.join("; ") : data.error || "丢弃失败";
          showToast(detail, "error");
          return;
        }
        if (poDraft.value && poDraft.value.id === draftId) {
          poDraft.value = null;
        }
        if (invoiceDraft.value && invoiceDraft.value.id === draftId) {
          invoiceDraft.value = null;
        }
        showToast("草稿已丢弃");
      } catch (e) {
        showToast("丢弃错误: " + e.message, "error");
      }
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

    async function loadRecalcNotes() {
      const data = await api(`/api/batches/${currentBatch.value.id}/recalc-notes`);
      if (data) {
        recalcNotes.value = data.notes || [];
      }
      loadComparisons();
    }

    async function doCompare() {
      compareError.value = "";
      comparisonResult.value = null;
      if (!canCompare.value) {
        compareError.value = "请选择两个不同的版本进行对比";
        return;
      }
      try {
        const res = await fetch(`/api/batches/${currentBatch.value.id}/recalc-notes/compare`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            note_a_id: Number(compareForm.noteAId),
            note_b_id: Number(compareForm.noteBId),
            operator: "web_user",
          }),
        });
        const data = await res.json();
        if (!res.ok) {
          compareError.value = data.error || "对比失败";
          showToast(data.error || "对比失败", "error");
          return;
        }
        comparisonResult.value = data.comparison;
        reviewForm.remark = data.comparison.review_remark || "";
        showToast("对比完成");
        loadComparisons();
      } catch (e) {
        compareError.value = "网络错误: " + e.message;
        showToast("网络错误: " + e.message, "error");
      }
    }

    async function loadComparisons() {
      if (!currentBatch.value) return;
      let url = `/api/batches/${currentBatch.value.id}/recalc-notes/comparisons`;
      if (comparisonFilter.value) {
        url += `?review_status=${comparisonFilter.value}`;
      }
      const data = await api(url);
      if (data) {
        comparisonHistory.value = data.comparisons || [];
      }
    }

    function loadComparisonDetail(c) {
      comparisonResult.value = c;
      reviewForm.remark = c.review_remark || "";
      compareForm.noteAId = String(c.note_a_id);
      compareForm.noteBId = String(c.note_b_id);
    }

    async function doReview(status) {
      if (!comparisonResult.value) return;
      try {
        const res = await fetch(`/api/batches/${currentBatch.value.id}/recalc-notes/comparisons/${comparisonResult.value.id}/review`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            review_status: status,
            review_remark: reviewForm.remark,
            operator: "web_user",
          }),
        });
        const data = await res.json();
        if (!res.ok) {
          showToast(data.error || "复核操作失败", "error");
          return;
        }
        comparisonResult.value = data.comparison;
        showToast("复核操作成功");
        loadComparisons();
      } catch (e) {
        showToast("网络错误: " + e.message, "error");
      }
    }

    function toggleComparisonSelect(id) {
      const s = new Set(selectedComparisonIds.value);
      if (s.has(id)) s.delete(id);
      else s.add(id);
      selectedComparisonIds.value = s;
    }

    function toggleSelectAllComparisons() {
      const pending = comparisonHistory.value.filter(c => c.review_status === "PENDING");
      const allSelected = pending.every(c => selectedComparisonIds.value.has(c.id));
      const s = new Set(selectedComparisonIds.value);
      if (allSelected) {
        pending.forEach(c => s.delete(c.id));
      } else {
        pending.forEach(c => s.add(c.id));
      }
      selectedComparisonIds.value = s;
    }

    async function doBatchReview(status) {
      if (selectedComparisonIds.value.size === 0) {
        showToast("请先勾选要批量操作的记录", "error");
        return;
      }
      batchConflictResult.value = null;
      try {
        const res = await fetch(`/api/batches/${currentBatch.value.id}/recalc-notes/comparisons/batch-review`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            comparison_ids: Array.from(selectedComparisonIds.value),
            review_status: status,
            review_remark: batchReviewForm.remark,
            operator: "web_user",
          }),
        });
        const data = await res.json();
        if (!res.ok) {
          showToast(data.error || "批量复核操作失败", "error");
          return;
        }
        if (data.conflict_count > 0) {
          batchConflictResult.value = data;
          showToast(`批量复核：成功 ${data.success_count} 条，冲突 ${data.conflict_count} 条`, data.success_count > 0 ? "success" : "error");
        } else {
          showToast(`批量复核成功，共 ${data.success_count} 条`);
        }
        selectedComparisonIds.value = new Set();
        batchReviewForm.remark = "";
        loadComparisons();
        if (comparisonResult.value) {
          const r = await fetch(`/api/batches/${currentBatch.value.id}/recalc-notes/comparisons/${comparisonResult.value.id}`);
          if (r.ok) {
            const d = await r.json();
            comparisonResult.value = d.comparison;
          }
        }
      } catch (e) {
        showToast("网络错误: " + e.message, "error");
      }
    }

    function reviewStatusLabel(s) {
      const m = { PENDING: "待复核", CONFIRMED: "已确认", IGNORED: "已忽略" };
      return m[s] || s;
    }

    function reviewStatusClass(s) {
      const m = {
        PENDING: "bg-yellow-100 text-yellow-700 px-2 py-0.5 rounded text-xs font-semibold",
        CONFIRMED: "bg-green-100 text-green-700 px-2 py-0.5 rounded text-xs font-semibold",
        IGNORED: "bg-gray-200 text-gray-600 px-2 py-0.5 rounded text-xs font-semibold",
      };
      return m[s] || "bg-gray-100 text-gray-700 px-2 py-0.5 rounded text-xs font-semibold";
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
      else if (tab === "recalc-notes") loadRecalcNotes();
    });

    onMounted(loadDashboard);

    return {
      recentBatches, currentBatch, matchResults, exceptions, toleranceHistory, auditLogs,
      batchSummary, activeTab, showCreateModal, toast, tabs,
      createForm, toleranceForm, compareForm, reviewForm,
      recalcNotes, comparisonResult, comparisonHistory, comparisonFilter, compareError,
      selectedComparisonIds, batchReviewForm, batchConflictResult,
      poDraft, invoiceDraft,
      canUpload, canMatch, canConfirm, canPost, canRollback, canReset, canCompare, hasSelectedComparisons,
      openBatch, createBatch, precheckFile, handleDrop, updateTolerance, runMatch,
      saveRemark, resolveException, confirmBatch, postBatch, rollbackBatch, resetBatch,
      exportReport, loadRecalcNotes, doCompare, loadComparisons, loadComparisonDetail, doReview,
      toggleComparisonSelect, toggleSelectAllComparisons, doBatchReview,
      confirmDraft, discardDraft,
      statusLabel, statusClass, matchTypeLabel, matchTypeClass, reviewStatusLabel, reviewStatusClass, formatTime,
      showToast,
    };
  },
}).mount("#app");
