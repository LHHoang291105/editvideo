'use strict';

const $ = id => document.getElementById(id);
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const pageSize = 25;
const defaultSlowSpeed = 0.8;
const defaultVoicePitches = {cute: 2, bright: 2, soft: 1};
const optionNames = ['tts_voice','tts_pitch','voice_speed','original_volume','voice_volume','ocr_device','sample_fps','roi_left','roi_top','roi_right','roi_bottom','gemini_model','blur_chinese_subtitles','blur_left','blur_top','blur_right','blur_bottom','blur_strength','mirror_video','burn_logo','logo_position','logo_width_percent','logo_opacity','logo_margin_percent','annotation_ocr_enabled','annotation_left','annotation_top','annotation_right','annotation_bottom','annotation_font_size','annotation_display_y'];
const strings = new Set(['tts_voice','tts_pitch','ocr_device','gemini_model','logo_position']);
const booleans = new Set(['blur_chinese_subtitles','mirror_video','burn_logo','annotation_ocr_enabled']);

let projects = [];
let active = null;
let rows = [];
let revision = 'missing';
let dirty = false;
let page = 0;
let job = null;
let cursor = 0;
let lastJobId = null;
let lastJobStatus = null;
let preview = null;
let polling = false;
let previewKind = 'original';
let chineseSrt = '';
let chineseSrtProject = null;
let annotationChineseSrt = '';
let annotationChineseSrtProject = null;
let toastTimer;
let annotationDrawMode = false;
let annotationStart = null;

function toast(message, error = false) {
  $('toast').textContent = message;
  $('toast').className = error ? 'error' : '';
  $('toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, error ? 11000 : 5000);
}

async function api(path, data) {
  const response = await fetch(path, data === undefined ? {} : {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
    body: JSON.stringify(data),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || 'Không kết nối được app.');
  return payload;
}

function handle(fn) {
  return async event => {
    try {
      await fn(event);
    } catch (error) {
      toast(error.message, true);
    }
  };
}

function confirmAction(title, message) {
  $('confirm-title').textContent = title;
  $('confirm-message').textContent = message;
  $('confirm-dialog').showModal();
  return new Promise(resolve => {
    const finish = yes => {
      $('confirm-dialog').close();
      resolve(yes);
    };
    $('confirm-yes').onclick = () => finish(true);
    $('confirm-no').onclick = () => finish(false);
    $('confirm-dialog').oncancel = () => resolve(false);
  });
}

function projectUrl(suffix = '') {
  if (!active) throw new Error('Chọn một video trước.');
  return `/api/projects/${active.id}${suffix}`;
}

function busy() {
  return Boolean(job && ['running', 'stopping'].includes(job.status));
}

function setDirty(value) {
  dirty = value;
  $('dirty-state').textContent = value ? '● Chưa lưu thay đổi' : '';
}

function timeText(seconds) {
  const milliseconds = Math.round(seconds * 1000);
  return `${String(Math.floor(milliseconds / 3600000)).padStart(2, '0')}:${String(Math.floor(milliseconds / 60000) % 60).padStart(2, '0')}:${String(Math.floor(milliseconds / 1000) % 60).padStart(2, '0')},${String(milliseconds % 1000).padStart(3, '0')}`;
}

function sizeText(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let amount = bytes / 1024;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index += 1;
  }
  return `${amount.toFixed(1)} ${units[index]}`;
}

function projectFile(kind) {
  if (!active?.files) return {exists: false, name: ''};
  // Older records used only "source". Their original preview remains usable.
  if (kind === 'original') return active.files.original || active.files.source || {exists: false, name: ''};
  return active.files[kind] || {exists: false, name: ''};
}

function hasFile(kind) {
  return projectFile(kind).exists === true;
}

function sourceState() {
  const state = active?.source_state;
  return ['original', 'review_required', 'approved'].includes(state) ? state : 'original';
}

function requiresSlowReview() {
  return sourceState() === 'review_required';
}

function projectSlowSpeed() {
  const raw = active?.slow_speed ?? active?.options?.source_speed;
  const value = raw === null || raw === undefined ? Number.NaN : Number(raw);
  return Number.isFinite(value) ? value : defaultSlowSpeed;
}

function sourceError() {
  return typeof active?.source_error === 'string' && active.source_error.trim() ? active.source_error.trim() : '';
}

function pipelineBlockedBySource() {
  return requiresSlowReview() || Boolean(sourceError());
}

function hasReadySlow() {
  return active?.slow_ready === true && hasFile('slow');
}

function selectedSlowSpeed() {
  const value = Number($('slow_speed').value);
  return Number.isFinite(value) ? value : defaultSlowSpeed;
}

function speedText(speed) {
  return `${Number(speed).toFixed(2)}×`;
}

async function copyText(text) {
  if (!text.trim()) throw new Error('SRT Trung đang trống.');
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch (_) {
      // Fall back for older loopback browser contexts.
    }
  }
  const field = document.createElement('textarea');
  field.value = text;
  field.setAttribute('readonly', '');
  field.style.cssText = 'position:fixed;left:-9999px;top:0';
  document.body.append(field);
  field.select();
  try {
    if (!document.execCommand('copy')) throw new Error('Trình duyệt không cho chép vào bộ nhớ tạm.');
  } finally {
    field.remove();
  }
}

async function refreshProjects() {
  projects = (await api('/api/projects')).projects;
  renderProjects();
  if (active) {
    active = projects.find(project => project.id === active.id) || null;
    updateFiles();
  }
}

function renderProjects() {
  $('project-list').replaceChildren();
  for (const project of projects) {
    const button = document.createElement('button');
    button.className = `project-item${active?.id === project.id ? ' selected' : ''}`;
    button.textContent = project.name;
    button.title = project.name;
    button.onclick = handle(() => selectProject(project.id));
    $('project-list').append(button);
  }
}

async function selectProject(id) {
  if (dirty && !await confirmAction('Rời bản đang sửa?', 'Các thay đổi chưa lưu sẽ mất. Bạn có muốn chuyển video?')) return;
  active = projects.find(project => project.id === id);
  if (!active) return;
  chineseSrt = '';
  chineseSrtProject = null;
  annotationChineseSrt = '';
  annotationChineseSrtProject = null;
  localStorage.setItem('vietdub-project', id);
  renderProjects();
  $('empty-state').hidden = true;
  $('workspace').hidden = false;
  $('project-name').textContent = active.name;
  $('project-kind').textContent = active.legacy ? 'Dữ liệu CLI' : 'Project riêng';
  $('cleanup-project').textContent = `Dữ liệu · ${active.name}`;
  for (const key of optionNames) {
    const value = active.options?.[key];
    if (booleans.has(key)) $(key).checked = Boolean(value);
    else if (value !== undefined && value !== null) $(key).value = value;
  }
  renderEdgeVoice();
  $('tts_pitch').value = String(ttsPitchNumber(active.options?.tts_pitch));
  updateTtsPitch();
  updateLogoLabels();
  renderLogoControls();
  $('slow_speed').value = projectSlowSpeed().toFixed(2);
  updateSpeed();
  updateSlowSpeed();
  updateFiles();
  const initialKind = hasReadySlow() && ['review_required', 'approved'].includes(sourceState()) ? 'slow' : 'original';
  showVideo(initialKind);
  await loadSubtitles();
  invalidatePreview();
  switchTab('studio');
}

function updateSpeed() {
  $('speed-label').textContent = `${Number($('voice_speed').value).toFixed(2)}×`;
}

function ttsPitchNumber(value) {
  const matched = typeof value === 'string' && value.match(/^([+-])(\d{1,2})Hz$/);
  const number = matched ? Number(`${matched[1]}${matched[2]}`) : 0;
  return Math.max(-20, Math.min(20, number));
}

function ttsPitchSetting() {
  const number = Math.trunc(Number($('tts_pitch').value));
  return `${number >= 0 ? '+' : ''}${number}Hz`;
}

function updateTtsPitch() {
  $('tts-pitch-label').textContent = `${ttsPitchSetting().replace('Hz', '')} Hz`;
}

function logoNumber(id, minimum, maximum, fallback) {
  const value = Number($(id).value);
  return Number.isFinite(value) ? Math.max(minimum, Math.min(maximum, value)) : fallback;
}

function updateLogoLabels() {
  $('logo-width-label').textContent = `${Math.round(logoNumber('logo_width_percent', 2, 50, 12))}%`;
  $('logo-opacity-label').textContent = `${Math.round(logoNumber('logo_opacity', 0.05, 1, 1) * 100)}%`;
  $('logo-margin-label').textContent = `${Math.round(logoNumber('logo_margin_percent', 0, 20, 3))}%`;
}

function renderLogoControls() {
  if (!active) return;
  const file = projectFile('logo');
  const hasLogo = file.exists === true;
  const checkbox = $('burn_logo');
  const card = $('logo-card');
  const enabled = hasLogo && checkbox.checked;
  const fileName = typeof file.name === 'string' && file.name.trim() ? file.name.trim() : 'logo đã chọn';
  const state = $('logo-state');
  state.className = `logo-state ${enabled ? 'enabled' : hasLogo ? 'ready' : 'missing'}`;
  state.textContent = enabled ? 'Sẽ chèn khi xuất' : hasLogo ? 'Đã chọn logo' : 'Chưa có logo';
  $('logo-status').textContent = !hasLogo
    ? checkbox.checked
      ? 'Đã bật chèn logo nhưng không thấy file ảnh. Tải lại logo hoặc tắt lựa chọn này trước khi xuất video.'
      : 'Chưa chọn logo. Chọn ảnh PNG, JPG hoặc WebP trước khi bật chèn logo.'
    : enabled
      ? `Đang dùng ${fileName}. Logo sẽ xuất hiện khi tạo video cuối.`
      : `Đã chọn ${fileName}. Bật chèn logo khi muốn xuất video cuối.`;
  checkbox.disabled = busy() || (!hasLogo && !checkbox.checked);
  $('logo-upload').disabled = busy();
  for (const id of ['logo_position', 'logo_width_percent', 'logo_opacity', 'logo_margin_percent']) {
    $(id).disabled = busy() || !enabled;
  }
  card.classList.toggle('logo-enabled', enabled);
  card.classList.toggle('logo-missing', !hasLogo);
  $('logo-options').classList.toggle('is-disabled', !enabled);
  updateLogoLabels();
}

function annotationRegionFromForm() {
  return {
    left: Number($('annotation_left').value), top: Number($('annotation_top').value),
    right: Number($('annotation_right').value), bottom: Number($('annotation_bottom').value),
  };
}

function validAnnotationRegion(region) {
  return Object.values(region).every(Number.isFinite)
    && region.left >= 0 && region.top >= 0 && region.right <= 1 && region.bottom <= 1
    && region.right > region.left && region.bottom > region.top;
}

function processingPreviewKind() {
  return sourceState() === 'approved' ? 'slow' : 'original';
}

function displayedVideoBounds() {
  const video = $('video');
  const wrap = $('video-wrap');
  if (!video.videoWidth || !video.videoHeight || video.hidden) return null;
  const outer = wrap.getBoundingClientRect();
  if (!outer.width || !outer.height) return null;
  const videoRatio = video.videoWidth / video.videoHeight;
  const outerRatio = outer.width / outer.height;
  const width = outerRatio > videoRatio ? outer.height * videoRatio : outer.width;
  const height = outerRatio > videoRatio ? outer.height : outer.width / videoRatio;
  return {
    left: outer.left + (outer.width - width) / 2,
    top: outer.top + (outer.height - height) / 2,
    width, height, outer,
  };
}

function renderAnnotationRegionBox(region = annotationRegionFromForm(), force = false) {
  const box = $('annotation-region-box');
  const bounds = displayedVideoBounds();
  const enabled = force || $('annotation_ocr_enabled').checked;
  if (!enabled || !bounds || !validAnnotationRegion(region)) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  box.style.left = `${bounds.left - bounds.outer.left + region.left * bounds.width}px`;
  box.style.top = `${bounds.top - bounds.outer.top + region.top * bounds.height}px`;
  box.style.width = `${(region.right - region.left) * bounds.width}px`;
  box.style.height = `${(region.bottom - region.top) * bounds.height}px`;
}

function regionPointFromPointer(event, clampToImage = false) {
  const bounds = displayedVideoBounds();
  if (!bounds) return null;
  const rawX = (event.clientX - bounds.left) / bounds.width;
  const rawY = (event.clientY - bounds.top) / bounds.height;
  if (!clampToImage && (rawX < 0 || rawX > 1 || rawY < 0 || rawY > 1)) return null;
  return {
    x: Math.max(0, Math.min(1, rawX)),
    y: Math.max(0, Math.min(1, rawY)),
  };
}

function regionBetween(start, end) {
  return {
    left: Math.min(start.x, end.x), top: Math.min(start.y, end.y),
    right: Math.max(start.x, end.x), bottom: Math.max(start.y, end.y),
  };
}

function endAnnotationDraw() {
  annotationDrawMode = false;
  annotationStart = null;
  $('annotation-overlay').classList.remove('drawing');
  $('annotation-draw-hint').hidden = true;
}

function annotationCopyButton() {
  let button = $('copy-annotation-zh');
  if (button) return button;
  // Waitress keeps compiled templates in memory.  Create this small control
  // when a browser reloads against an already-running server that still has
  // the prior template, so a user never needs to interrupt a long render just
  // to receive this client-side addition.
  const download = $('download-annotation-zh');
  const importLabel = $('import-annotation-label');
  const actions = download?.parentElement;
  if (!actions) return null;
  button = document.createElement('button');
  button.id = 'copy-annotation-zh';
  button.type = 'button';
  button.hidden = true;
  button.title = 'Chép SRT chú thích tiếng Trung vào bộ nhớ tạm';
  button.textContent = 'Chép SRT Trung';
  actions.insertBefore(button, importLabel || download);
  button.onclick = handle(copyAnnotationChineseSrt);
  return button;
}

function coverageCount(value) {
  if (Array.isArray(value)) return value.length;
  const count = Number(value);
  return Number.isInteger(count) && count >= 0 ? count : null;
}

function firstCoverageCount(source, keys) {
  for (const key of keys) {
    const count = coverageCount(source?.[key]);
    if (count !== null) return count;
  }
  return null;
}

function annotationCoverage(project = active) {
  // New servers expose `annotation_coverage`.  Keep the older
  // `annotation_translation` spelling as a compatibility path so a refreshed
  // browser can still describe an existing project accurately during an app
  // update.
  const roots = [project?.annotation_coverage, project?.annotation_translation];
  const candidates = roots.flatMap(value => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return [];
    return [value, ...(value.coverage && typeof value.coverage === 'object' && !Array.isArray(value.coverage)
      ? [value.coverage] : [])];
  });
  for (const source of candidates) {
    const total = firstCoverageCount(source, ['total', 'zh_count', 'source_count', 'source_total', 'zhCount']);
    const translated = firstCoverageCount(
      source, ['translated', 'matched_count', 'matched', 'translated_count', 'vi_count', 'viCount'],
    );
    const coverageGap = firstCoverageCount(source, ['coverage_gap_count', 'missing', 'untranslated', 'untranslated_count']);
    const sourceMissing = firstCoverageCount(source, ['missing_count', 'missing_ids']);
    const extra = firstCoverageCount(source, ['extra_count', 'extra', 'extras', 'extra_ids']);
    const timingMismatches = firstCoverageCount(
      source, ['timing_mismatch_count', 'timing_mismatches', 'mismatched_count', 'mismatches'],
    );
    const hasSignal = total !== null || translated !== null || missing !== null || extra !== null
      || timingMismatches !== null || source.available === false || typeof source.complete === 'boolean';
    if (!hasSignal) continue;
    return {
      available: source.available !== false,
      total,
      translated,
      missing: coverageGap,
      sourceMissing,
      extra: extra ?? 0,
      timingMismatches: timingMismatches ?? 0,
      complete: source.complete,
      empty: source.empty === true,
      status: typeof source.status === 'string' ? source.status : '',
    };
  }
  return null;
}

function annotationCoverageDetails(coverage = annotationCoverage()) {
  if (!coverage?.available || coverage.total === null || coverage.translated === null) return null;
  const total = coverage.total;
  const translated = Math.min(coverage.translated, total);
  const missing = coverage.missing ?? Math.max(0, total - translated);
  const sourceMissing = coverage.sourceMissing ?? missing;
  const complete = coverage.complete === true || (coverage.complete !== false
    && total > 0 && translated === total && missing === 0
    && coverage.extra === 0 && coverage.timingMismatches === 0);
  return {total, translated, missing, sourceMissing, extra: coverage.extra, timingMismatches: coverage.timingMismatches, complete};
}

function annotationCoverageProblem(details) {
  if (!details || details.complete) return '';
  const problems = [];
  if (details.sourceMissing > 0) problems.push(`${details.sourceMissing} câu Trung chưa có bản Việt`);
  else if (details.missing > 0 && details.timingMismatches === 0) problems.push(`${details.missing} câu chưa khớp để chèn`);
  if (details.timingMismatches > 0) problems.push(`${details.timingMismatches} mục lệch ID hoặc thời gian`);
  if (details.extra > 0) problems.push(`${details.extra} mục Việt không có cue Trung để đối chiếu`);
  return problems.join('; ') || 'SRT Việt chưa khớp hoàn toàn với SRT Trung';
}

function annotationCoverageEffect(details) {
  if (!details || details.complete) return '';
  const effects = [];
  if (details.sourceMissing > 0) effects.push('Các câu Trung thiếu bản Việt sẽ không hiện trên video.');
  if (details.timingMismatches > 0) effects.push('Mục lệch mốc vẫn chèn theo timestamp trong SRT Việt.');
  if (details.extra > 0) effects.push('Mục Việt không có cue Trung vẫn chèn theo timestamp của chính nó.');
  return effects.join(' ');
}

function annotationDisplayYText() {
  const raw = Number($('annotation_display_y').value);
  const percent = Number.isFinite(raw) ? Math.round(raw * 100) : 12;
  return `${percent}% từ trên`;
}

function updateAnnotationDisplayY() {
  $('annotation-display-y-label').textContent = annotationDisplayYText();
}

function annotationManualImportNotice(details = annotationCoverageDetails()) {
  if (!details) return 'Đã có SRT chú thích Việt. Gemini giữ nguyên file hiện có; dùng “Nhập SRT Việt” để thay bản khi cần.';
  return `SRT Việt khớp ${details.translated}/${details.total} câu. ${annotationCoverageProblem(details)}; hãy dùng “Nhập SRT Việt” để nhập lại bản SRT đầy đủ. Gemini giữ nguyên file hiện có.`;
}

function renderAnnotationControls() {
  if (!active) return;
  const enabled = $('annotation_ocr_enabled').checked;
  const drawingOnSource = previewKind === processingPreviewKind();
  const zhReady = hasFile('annotation_zh');
  const annotationZhCached = annotationChineseSrtProject === active.id && annotationChineseSrt.trim().length > 0;
  const viReady = hasFile('annotation_vi');
  const coverage = annotationCoverage();
  const coverageDetails = annotationCoverageDetails(coverage);
  const coverageIsEmpty = coverage?.available && coverageDetails?.total === 0;
  const coverageUnavailable = zhReady && viReady && coverage?.available === false;
  const translationComplete = viReady && coverageDetails?.complete === true;
  const translationIncomplete = viReady && coverageDetails && !coverageDetails.complete;
  // A Vietnamese annotation SRT can come straight from a GPT/manual video
  // review.  With no Chinese OCR SRT there is nothing to compare, but it is
  // fully renderable using its own timestamps.
  const directVietnamese = enabled && viReady && !zhReady;
  const annotationReady = translationComplete || directVietnamese;
  const directCueCount = coverageCount(coverage?.vi_count);
  const state = $('annotation-state');
  const card = $('annotation-card');
  state.className = `annotation-state ${!enabled ? 'missing' : annotationReady ? 'ready' : 'pending'}`;
  state.textContent = !enabled
    ? 'Chưa khoanh vùng'
    : directVietnamese
      ? `SRT Việt trực tiếp${directCueCount === null ? '' : ` · ${directCueCount} câu`}`
    : !zhReady
      ? 'Sẵn sàng OCR'
      : coverageUnavailable
        ? 'Không đọc được SRT'
      : coverageIsEmpty
          ? 'OCR chưa có chữ'
          : !viReady
            ? 'Chờ dịch'
            : translationComplete
              ? `Đã dịch ${coverageDetails.translated}/${coverageDetails.total} câu`
              : translationIncomplete
                ? coverageDetails.sourceMissing > 0
                  ? `Thiếu ${coverageDetails.sourceMissing}/${coverageDetails.total} câu`
                  : `Cần rà soát ${coverageDetails.translated}/${coverageDetails.total}`
                : 'Có SRT Việt';
  $('annotation-status').textContent = !enabled
    ? 'Bấm “Khoanh vùng trên video”, rồi kéo chuột qua vị trí muốn đặt chú thích. Sau đó có thể nhập SRT Việt do GPT tạo trực tiếp; không cần OCR Trung hoặc tạo giọng.'
    : directVietnamese
      ? `Đã có${directCueCount === null ? '' : ` ${directCueCount} câu`} SRT chú thích Việt trực tiếp. Nó sẽ chèn theo timestamp trong SRT, không cần OCR Trung, dịch Gemini hoặc TTS. Bấm “Chỉ chèn chú thích” để xuất MP4 giữ audio gốc.`
    : !zhReady
      ? 'Đã lưu vị trí trên khung hình. Chọn “Nhập SRT Việt từ GPT” để dùng trực tiếp, OCR Trung rồi dịch, hoặc OCR tăng cường nếu chữ dọc/cách điệu bị bỏ sót.'
      : coverageUnavailable
        ? 'Không thể đối chiếu SRT chú thích Trung và Việt. Hãy kiểm tra lại định dạng SRT UTF-8 rồi tải lại trang.'
      : coverageIsEmpty
          ? 'SRT chú thích Trung chưa có câu nào để dịch. Hãy khoanh sát dòng chữ hơn rồi OCR lại vùng này.'
          : !viReady
            ? 'Đã OCR SRT chú thích Trung. Bấm “Dịch chú thích” để tạo bản Việt trước khi xuất.'
            : translationComplete
              ? `SRT chú thích Việt khớp ${coverageDetails.translated}/${coverageDetails.total} câu Trung. Khi xuất video, câu dịch giữ ngang theo vùng đã khoanh và hiện ở ${annotationDisplayYText()}; TTS không đọc các câu này.`
              : translationIncomplete
                ? `SRT chú thích Việt khớp ${coverageDetails.translated}/${coverageDetails.total} câu. ${annotationCoverageProblem(coverageDetails)}. ${annotationCoverageEffect(coverageDetails)} Dùng “Nhập SRT Việt” để nhập lại bản SRT đầy đủ.`
                : 'Đã có SRT chú thích Việt. Chưa có dữ liệu đối chiếu số câu; tải lại trang sau khi app cập nhật để kiểm tra phần còn thiếu.';
  card.classList.toggle('annotation-ready', enabled && annotationReady);
  card.classList.toggle('annotation-incomplete', enabled && translationIncomplete);
  card.classList.toggle('annotation-missing', !enabled);
  $('draw-annotation').disabled = busy() || pipelineBlockedBySource()
    || !drawingOnSource || !displayedVideoBounds();
  $('clear-annotation').disabled = busy() || !enabled;
  $('annotation_ocr_enabled').disabled = busy() || !enabled;
  $('annotation_font_size').disabled = busy() || !enabled;
  $('annotation_display_y').disabled = busy() || !enabled;
  $('annotation-font-size-label').textContent = String(Math.round(Number($('annotation_font_size').value) || 28));
  updateAnnotationDisplayY();
  $('annotation-ocr').disabled = busy() || pipelineBlockedBySource() || !enabled;
  $('annotation-ocr-enhanced').disabled = busy() || pipelineBlockedBySource() || !enabled;
  $('annotation-translate').disabled = busy() || pipelineBlockedBySource() || !enabled || !zhReady;
  const copyAnnotationButton = annotationCopyButton();
  if (copyAnnotationButton) {
    copyAnnotationButton.hidden = !zhReady;
    copyAnnotationButton.disabled = busy() || !annotationZhCached;
  }
  const canImportAnnotation = !busy() && !pipelineBlockedBySource() && enabled;
  $('import-annotation-srt').disabled = !canImportAnnotation;
  $('import-annotation-label').classList.toggle('is-disabled', !canImportAnnotation);
  $('annotation-burn').disabled = busy() || pipelineBlockedBySource() || !enabled || !viReady;
  $('download-annotation-zh').hidden = !zhReady;
  $('download-annotation-zh').href = projectUrl('/files/annotation_zh?download=1');
  $('download-annotation-srt').hidden = !viReady;
  $('download-annotation-srt').href = projectUrl('/files/annotation_vi?download=1');
  renderAnnotationRegionBox();
}

function beginAnnotationDraw() {
  if (busy()) throw new Error('Có tác vụ đang chạy.');
  if (pipelineBlockedBySource()) throw new Error(sourceError() || 'Hãy duyệt video tua chậm trước khi khoanh vùng chú thích.');
  const sourceKind = processingPreviewKind();
  if (previewKind !== sourceKind) {
    showVideo(sourceKind);
    toast('Đã chuyển về video nguồn dùng cho OCR. Bấm khoanh vùng thêm một lần khi khung hình tải xong.');
    return;
  }
  if (!displayedVideoBounds()) throw new Error('Video đang tải. Hãy chờ khung hình xuất hiện rồi thử lại.');
  $('video').pause();
  annotationDrawMode = true;
  annotationStart = null;
  $('annotation-overlay').classList.add('drawing');
  $('annotation-draw-hint').hidden = false;
  $('annotation-region-box').hidden = true;
  toast('Kéo chuột trực tiếp trên phần hình video để khoanh vị trí chú thích. Vùng này chỉ đặt chữ nếu bạn nhập SRT Việt trực tiếp.');
}

async function clearAnnotationRegion() {
  endAnnotationDraw();
  $('annotation_ocr_enabled').checked = false;
  await saveOptions();
  await refreshProjects();
  renderAnnotationControls();
  toast('Đã tắt vùng chú thích. Các SRT chú thích cũ được giữ riêng và không chèn vào video.');
}

async function importAnnotationSrt() {
  if (busy()) throw new Error('Có tác vụ đang chạy.');
  if (!$('annotation_ocr_enabled').checked) {
    throw new Error('Hãy khoanh vùng vị trí chú thích trên video trước khi nhập SRT Việt.');
  }
  const input = $('import-annotation-srt');
  const file = input.files[0];
  if (!file) return;
  try {
    if (file.size > 5_000_000) throw new Error('SRT chú thích quá lớn.');
    const content = await file.text();
    const data = await api(projectUrl('/import-annotation-srt'), {content});
    await refreshProjects();
    renderAnnotationControls();
    const direct = !hasFile('annotation_zh');
    const details = annotationCoverageDetails();
    const warning = data.warning || (!direct && details && !details.complete ? annotationCoverageProblem(details) : '');
    toast(warning
      ? `Đã lưu ${data.saved} câu chú thích Việt. ${warning} Bản cũ được sao lưu nếu đã có.`
      : `Đã lưu ${data.saved} câu chú thích Việt trực tiếp. Không cần OCR hoặc Gemini. Bản cũ được sao lưu nếu đã có.`);
  } finally {
    input.value = '';
  }
}

async function finishAnnotationDraw(event) {
  if (!annotationDrawMode || !annotationStart) return;
  const end = regionPointFromPointer(event, true);
  if (!end) {
    endAnnotationDraw();
    renderAnnotationControls();
    return;
  }
  const region = regionBetween(annotationStart, end);
  if (region.right - region.left < .015 || region.bottom - region.top < .015) {
    endAnnotationDraw();
    renderAnnotationControls();
    throw new Error('Vùng khoanh quá nhỏ. Hãy kéo đủ rộng cho chú thích Việt.');
  }
  for (const [key, value] of Object.entries(region)) $(
    `annotation_${key}`
  ).value = value.toFixed(6);
  $('annotation_ocr_enabled').checked = true;
  endAnnotationDraw();
  renderAnnotationRegionBox(region, true);
  await saveOptions();
  await refreshProjects();
  renderAnnotationControls();
  toast('Đã lưu vị trí chú thích. Bạn có thể nhập SRT Việt từ GPT ngay, OCR Trung, hoặc OCR tăng cường cho chữ dọc/cách điệu.');
}

function edgeVoices() {
  return Array.isArray(active?.edge_voices) ? active.edge_voices.filter(voice =>
    voice && typeof voice.id === 'string' && typeof voice.label === 'string') : [];
}

function selectedEdgeVoice() {
  return edgeVoices().find(voice => voice.id === $('tts_voice').value) || null;
}

function renderEdgeVoice() {
  if (!active) return;
  const voices = edgeVoices();
  const currentId = active.options?.tts_voice;
  const select = $('tts_voice');
  select.replaceChildren();
  for (const voice of voices) {
    const option = document.createElement('option');
    option.value = voice.id;
    option.textContent = voice.label;
    select.append(option);
  }
  if (voices.some(voice => voice.id === currentId)) {
    select.value = currentId;
    const selected = selectedEdgeVoice();
    $('edge-voice-note').textContent = selected?.id === 'vi-VN-HoaiMyNeural'
      ? 'Hoài My là giọng nữ tiếng Việt duy nhất Edge hiện cung cấp. “Cute/mềm/tự nhiên” không còn hiện vì chúng không phải giọng mới. Nghe thử cần Internet.'
      : 'Nam Minh là giọng nam tiếng Việt Edge hiện cung cấp. Đây là một người nói khác với Hoài My. Nghe thử cần Internet.';
  } else {
    const custom = document.createElement('option');
    custom.value = currentId || '';
    custom.textContent = `Giọng tùy chỉnh từ .env${currentId ? ` (${currentId})` : ''}`;
    select.append(custom);
    select.value = custom.value;
    $('edge-voice-note').textContent = 'Giữ giọng tùy chỉnh từ .env. Edge hiện chỉ có Hoài My nữ và Nam Minh nam cho tiếng Việt; nghe thử các giọng này cần Internet.';
  }
  $('preview-tts-voice').disabled = busy() || !selectedEdgeVoice();
}

async function applyEdgeVoice() {
  if (busy()) throw new Error('Có tác vụ đang chạy.');
  const voice = selectedEdgeVoice();
  if (!voice) throw new Error('Chọn một giọng Edge hợp lệ.');
  await saveOptions();
  renderEdgeVoice();
  toast(`Đã chọn ${voice.label}.`);
}

async function previewTtsVoice() {
  if (busy()) throw new Error('Có tác vụ đang chạy.');
  const voice = selectedEdgeVoice();
  if (!voice) throw new Error('Chọn một giọng Edge trước khi nghe thử.');
  const button = $('preview-tts-voice');
  button.disabled = true;
  try {
    await saveOptions();
    const data = await api(projectUrl('/tts-preview'), {voice: voice.id});
    const player = $('tts-voice-preview');
    player.hidden = false;
    player.src = `${data.url}?v=${Date.now()}`;
    player.load();
    try { await player.play(); } catch (_) { /* Browser still shows a play control. */ }
    toast(`Đang nghe thử ${data.voice.label}.`);
  } finally {
    renderEdgeVoice();
  }
}

function updateSlowSpeed() {
  $('slow-speed-label').textContent = speedText(selectedSlowSpeed());
  renderSlowWorkflow();
}

function voiceState() {
  return active?.voice || {
    options: {source_mode: 'project', model_id: '', preset: 'cute', speaker_id: null,
      pitch_semitones: null, post_pitch_semitones: 0},
    source: {name: '', exists: false}, models: [], runtime: {ready: false, errors: []},
    result_ready: false, result_stale: false, audio: {exists: false}, video: {exists: false},
  };
}

function voicePitchText(value) {
  const number = Number(value);
  return `${number >= 0 ? '+' : ''}${number.toFixed(Number.isInteger(number) ? 0 : 1)} bán âm`;
}

function updateVoicePitchLabels() {
  $('voice-pitch-label').textContent = voicePitchText($('voice_pitch').value);
  $('voice-post-pitch-label').textContent = voicePitchText($('voice_post_pitch').value);
}

function clearRvcModelPreview() {
  const player = $('rvc-model-preview');
  player.pause();
  player.hidden = true;
  player.removeAttribute('src');
  player.load();
}

function renderVoiceCard() {
  if (!active) return;
  const voice = voiceState();
  const options = voice.options || {};
  const mode = options.source_mode === 'upload' ? 'upload' : 'project';
  const preset = ['cute', 'bright', 'soft'].includes(options.preset) ? options.preset : 'cute';
  const models = Array.isArray(voice.models) ? voice.models : [];
  const selectedModel = typeof options.model_id === 'string' ? options.model_id : '';
  const selector = $('voice_model');
  selector.replaceChildren();
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = models.length ? 'Chọn model RVC .pth' : 'Chưa thêm model .pth';
  selector.append(placeholder);
  for (const model of models) {
    const item = document.createElement('option');
    item.value = model.id;
    const label = typeof model.label === 'string' && model.label.trim() ? model.label : model.name;
    item.textContent = `${label}${model.has_index ? ' · index' : ''}`;
    selector.append(item);
  }
  selector.value = models.some(model => model.id === selectedModel) ? selectedModel : '';
  const currentModel = models.find(model => model.id === selector.value) || null;
  $('voice_source_mode').value = mode;
  $('voice_preset').value = preset;
  $('voice_speaker_id').value = options.speaker_id === null || options.speaker_id === undefined ? '' : options.speaker_id;
  $('voice_pitch').value = options.pitch_semitones === null || options.pitch_semitones === undefined
    ? defaultVoicePitches[preset] : options.pitch_semitones;
  $('voice_post_pitch').value = Number(options.post_pitch_semitones || 0);
  updateVoicePitchLabels();
  $('voice-upload-label').hidden = mode !== 'upload';
  const source = voice.source || {name: '', exists: false};
  $('voice-source-status').textContent = mode === 'project'
    ? `Dùng audio của ${source.name || 'video project'}${source.exists ? '.' : ' (không tìm thấy file).'}`
    : source.exists ? `Đã chọn: ${source.name}` : 'Chưa tải file WAV, MP3 hoặc MP4.';
  const runtime = voice.runtime || {ready: false, errors: []};
  const runtimeBadge = $('voice-runtime');
  runtimeBadge.className = `pill ${runtime.ready ? 'mint' : 'voice-runtime-error'}`;
  runtimeBadge.textContent = runtime.ready ? 'RVC GPU đã cài' : 'Cần cài RVC GPU';
  const errors = Array.isArray(runtime.errors) ? runtime.errors : [];
  const defaultModelNote = models.length
    ? 'Chọn model bạn có quyền sử dụng. CUDA được kiểm tra trước khi chạy; index cùng tên giúp giữ màu giọng rõ hơn.'
    : 'Chưa có model cài sẵn. Nếu sau này cần, chép file .pth bạn có quyền sử dụng vào models\\rvc; thêm .index cùng tên nếu có.';
  const selectedDescription = typeof currentModel?.description === 'string' ? currentModel.description.trim() : '';
  $('voice-model-note').textContent = runtime.ready
    ? (selectedDescription ? `${selectedDescription} ${defaultModelNote}` : defaultModelNote)
    : (errors.join(' ') || 'Chạy tools\\setup_rvc_gpu.ps1 để cài RVC + RMVPE CUDA.');
  const rvcPreviewNote = $('rvc-model-preview-note');
  if (!currentModel) {
    rvcPreviewNote.textContent = 'Chọn một model .pth trước khi nghe thử.';
  } else if (selectedDescription) {
    rvcPreviewNote.textContent = `${selectedDescription} Mẫu dùng câu Việt cố định qua RVC + RMVPE trên GPU; câu nguồn Edge cần Internet.`;
  } else {
    rvcPreviewNote.textContent = 'Mẫu dùng câu Việt cố định qua RVC + RMVPE trên GPU; câu nguồn Edge cần Internet.';
  }
  const resultReady = voice.result_ready === true;
  const resultStatus = $('voice-result-status');
  resultStatus.textContent = resultReady ? 'Đã có kết quả khớp với nguồn và model hiện tại.'
    : voice.result_stale ? 'Kết quả cũ không còn khớp nguồn hoặc model; hãy chạy lại.' : '';
  const audioReady = resultReady && voice.audio?.exists === true;
  const videoReady = resultReady && voice.video?.exists === true;
  const player = $('voice-preview');
  player.hidden = !audioReady;
  if (audioReady) player.src = projectUrl(`/files/voice_audio?v=${Date.now()}`);
  else {
    player.removeAttribute('src');
    player.load();
  }
  $('download-voice-audio').hidden = !audioReady;
  $('download-voice-audio').href = projectUrl('/files/voice_audio?download=1');
  $('download-voice-video').hidden = !videoReady;
  $('download-voice-video').href = projectUrl('/files/voice_video?download=1');
  const blockedForSource = mode === 'project' && pipelineBlockedBySource();
  $('convert-voice').disabled = busy() || !runtime.ready || !source.exists || !selector.value || blockedForSource;
  $('preview-rvc-model').disabled = busy() || !runtime.ready || !selector.value;
  for (const id of ['voice_source_mode', 'voice_model', 'voice_preset', 'voice_speaker_id', 'voice_pitch', 'voice_post_pitch', 'voice-upload']) {
    $(id).disabled = busy();
  }
}

function voiceOptionsFromForm() {
  const speaker = $('voice_speaker_id').value.trim();
  return {
    source_mode: $('voice_source_mode').value,
    model_id: $('voice_model').value,
    preset: $('voice_preset').value,
    speaker_id: speaker ? Number(speaker) : null,
    pitch_semitones: Number($('voice_pitch').value),
    post_pitch_semitones: Number($('voice_post_pitch').value),
  };
}

async function saveVoiceOptions() {
  const data = await api(projectUrl('/voice/options'), voiceOptionsFromForm());
  active = data.project;
  renderVoiceCard();
}

async function saveVoiceOptionsAfterPreviewChange() {
  clearRvcModelPreview();
  await saveVoiceOptions();
}

async function previewRvcModel() {
  if (busy()) throw new Error('Có tác vụ đang chạy.');
  const voice = voiceState();
  if (!voice.runtime?.ready) throw new Error((voice.runtime?.errors || ['Chưa cài RVC GPU.']).join(' '));
  if (!$('voice_model').value) throw new Error('Chọn model RVC .pth trước khi nghe thử.');
  const button = $('preview-rvc-model');
  button.disabled = true;
  try {
    // Persist exactly the model/preset/F0 values that the preview will use.
    await saveVoiceOptions();
    const data = await api(projectUrl('/voice/preview'), {});
    const player = $('rvc-model-preview');
    player.hidden = false;
    player.src = `${data.url}?v=${Date.now()}`;
    player.load();
    try { await player.play(); } catch (_) { /* Browser still shows a play control. */ }
    const label = typeof data.model?.label === 'string' ? data.model.label : 'model RVC đã chọn';
    $('rvc-model-preview-note').textContent = `Đang nghe mẫu thật từ ${label}.`;
    toast(`Đang nghe mẫu ${label}.`);
  } finally {
    renderVoiceCard();
  }
}

async function uploadVoiceSource() {
  const file = $('voice-upload').files[0];
  if (!file) return;
  if (!['.wav', '.mp3', '.mp4'].some(suffix => file.name.toLowerCase().endsWith(suffix))) {
    throw new Error('Chọn file WAV, MP3 hoặc MP4.');
  }
  const form = new FormData();
  form.append('voice', file);
  $('voice-upload').disabled = true;
  try {
    const response = await fetch(projectUrl('/voice/upload'), {
      method: 'POST', headers: {'X-CSRF-Token': csrf}, body: form,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Tải file nguồn đổi giọng thất bại.');
    active = data.project;
    await refreshProjects();
    renderVoiceCard();
    toast('Đã lưu file nguồn vào project.');
  } finally {
    $('voice-upload').value = '';
    renderVoiceCard();
  }
}

async function uploadLogo() {
  const input = $('logo-upload');
  const file = input.files[0];
  if (!file) return;
  if (!/\.(png|jpe?g|webp)$/i.test(file.name)) {
    throw new Error('Chọn ảnh logo PNG, JPG hoặc WebP.');
  }
  const form = new FormData();
  form.append('logo', file);
  input.disabled = true;
  try {
    const response = await fetch(projectUrl('/logo/upload'), {
      method: 'POST', headers: {'X-CSRF-Token': csrf}, body: form,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Tải logo vào project thất bại.');
    active = data.project;
    await refreshProjects();
    renderLogoControls();
    toast('Đã lưu logo vào project. Bật chèn logo khi cần xuất video.');
  } finally {
    input.value = '';
    renderLogoControls();
  }
}

async function startVoiceConversion() {
  if (busy()) throw new Error('Có tác vụ đang chạy.');
  if (dirty) throw new Error('Hãy lưu phụ đề đang sửa trước khi đổi giọng.');
  const voice = voiceState();
  if (voice.options?.source_mode === 'project' && pipelineBlockedBySource()) {
    throw new Error(sourceError() || 'Hãy duyệt video tua chậm trước khi dùng audio của video project.');
  }
  if (!voice.runtime?.ready) throw new Error((voice.runtime?.errors || ['Chưa cài RVC GPU.']).join(' '));
  if (!voice.source?.exists || !$('voice_model').value) throw new Error('Chọn nguồn audio và model RVC trước khi chạy.');
  await saveVoiceOptions();
  const response = await api(projectUrl('/jobs'), {action: 'voice', burn: false, reocr: false, api_key: ''});
  job = response.job;
  lastJobId = job.id;
  lastJobStatus = job.status;
  cursor = 0;
  $('job-log').textContent = '';
  renderJob();
  toast('Đang đổi giọng bằng RVC + RMVPE trên GPU.');
}

function renderSlowWorkflow() {
  if (!active) return;
  const state = sourceState();
  const hasSlow = hasReadySlow();
  const slowProblem = sourceError();
  const renderedSpeed = projectSlowSpeed();
  const requestedSpeed = selectedSlowSpeed();
  const isNewSpeed = Math.abs(renderedSpeed - requestedSpeed) > 0.001;
  const status = $('slow-status');
  const badge = $('slow-state');
  const card = $('slow-card');

  badge.className = `slow-state ${slowProblem ? 'error' : state}`;
  card.classList.toggle('review-required', state === 'review_required');
  card.classList.toggle('source-approved', state === 'approved');
  card.classList.toggle('source-error', Boolean(slowProblem));
  if (slowProblem) {
    badge.textContent = 'Cần tạo lại';
    status.textContent = `Bản tua chậm cũ không còn dùng được: ${slowProblem}. Hãy tạo lại trước khi duyệt hoặc OCR.`;
  } else if (state === 'review_required') {
    badge.textContent = 'Chờ duyệt';
    status.textContent = `Đã tạo bản ${speedText(renderedSpeed)}. Hãy xem trước khi OCR, phụ đề, giọng và video cuối dùng timeline này.`;
    $('slow-review-title').textContent = `Đã tạo video tua chậm ${speedText(renderedSpeed)}`;
    $('slow-review-message').textContent = 'Phát video để kiểm tra. Khi xác nhận, OCR sẽ lấy mốc thời gian từ bản này; phụ đề, giọng Việt và video cuối sẽ cùng timeline.';
  } else if (state === 'approved') {
    badge.textContent = `Đang dùng ${speedText(renderedSpeed)}`;
    status.textContent = isNewSpeed
      ? `Đang dùng bản ${speedText(renderedSpeed)} đã duyệt. Bạn đang chọn ${speedText(requestedSpeed)}; hãy tạo lại để xem và duyệt bản mới.`
      : `Đang dùng bản ${speedText(renderedSpeed)} đã duyệt cho OCR, phụ đề, giọng và video cuối.`;
  } else {
    badge.textContent = 'Video gốc';
    status.textContent = hasSlow
      ? `Video gốc đang được dùng. Có sẵn bản chậm ${speedText(renderedSpeed)} để bạn tạo lại hoặc duyệt.`
      : 'Tạo bản chậm để xem trước. Các bước sau chỉ dùng bản này khi bạn duyệt.';
  }

  $('slow-review').hidden = state !== 'review_required';
  $('source-gate').hidden = !pipelineBlockedBySource();
  if (slowProblem) {
    $('source-gate-title').textContent = 'Video đầu vào cần được tạo lại.';
    $('source-gate-message').textContent = `${slowProblem} Hãy tạo lại video tua chậm trước khi OCR, phụ đề, giọng hoặc xuất video.`;
  } else {
    $('source-gate-title').textContent = 'Đang chờ bạn duyệt video tua chậm.';
    $('source-gate-message').textContent = 'Hãy xem ở khung hình phía trên rồi chọn “Đã xem — dùng video này” trước khi OCR, sửa phụ đề hoặc tạo giọng.';
  }
  $('create-slow').textContent = hasSlow || slowProblem ? 'Tạo lại video tua chậm để xem' : 'Tạo video tua chậm để xem';
  $('create-slow').disabled = busy() || state === 'approved';
  $('slow_speed').disabled = busy() || state === 'approved';
  $('slow-approved-actions').hidden = state !== 'approved' && !slowProblem;
  $('slow-approved-note').textContent = slowProblem
    ? 'Bỏ bản chậm cũ để tạo lại video đầu vào hợp lệ.'
    : 'Bản chậm đã duyệt đang là video đầu vào.';
  $('return-original').textContent = slowProblem ? 'Bỏ bản chậm cũ' : 'Dùng lại video gốc';
  $('return-original').disabled = busy();
  $('approve-slow').disabled = busy() || !hasSlow;
  $('use-original').disabled = busy();
}

function updateFiles() {
  if (!active) return;
  const zhExists = hasFile('zh');
  const zhReady = chineseSrtProject === active.id && chineseSrt.trim().length > 0;
  $('download-srt').hidden = !hasFile('vi');
  $('download-srt').href = projectUrl('/files/vi?download=1');
  $('copy-zh-srt').hidden = !zhExists;
  $('copy-zh-srt').disabled = !zhReady || busy();
  $('show-original').disabled = !hasFile('original');
  $('show-slow').disabled = !hasReadySlow();
  $('show-final').disabled = !hasFile('final');
  if (!hasFile('final')) $('save-final').hidden = true;
  if (!hasFile(previewKind)) previewKind = hasFile('original') ? 'original' : 'slow';
  renderSlowWorkflow();
  renderVoiceCard();
  renderLogoControls();
  renderAnnotationControls();
}

function previewCaption(kind) {
  if (kind === 'final') return 'Video đã lồng tiếng';
  if (kind === 'slow') return `Video tua chậm · ${speedText(projectSlowSpeed())}`;
  return `Video gốc · ${active.name}`;
}

function emptyVideoMessage(kind) {
  if (kind === 'slow') return 'Video tua chậm chưa được tạo.';
  if (kind === 'final') return 'Video kết quả chưa được tạo.';
  return 'Video gốc không còn ở đường dẫn đã lưu.';
}

function showVideo(kind) {
  if (!active) return;
  if (kind === 'slow' && !hasReadySlow()) kind = 'original';
  const file = projectFile(kind);
  previewKind = kind;
  const exists = file.exists === true;
  const video = $('video');
  $('video-empty').hidden = exists;
  $('video-empty').textContent = emptyVideoMessage(kind);
  video.hidden = !exists;
  if (exists) video.src = projectUrl(`/files/${kind}?v=${Date.now()}`);
  else {
    video.removeAttribute('src');
    video.load();
  }
  $('show-original').classList.toggle('selected', kind === 'original');
  $('show-slow').classList.toggle('selected', kind === 'slow');
  $('show-final').classList.toggle('selected', kind === 'final');
  $('preview-caption').textContent = previewCaption(kind);
  const final = kind === 'final' && hasFile('final');
  $('download-video').hidden = !final;
  $('download-video').href = projectUrl('/files/final?download=1');
  $('save-final').hidden = !final;
  $('save-final').disabled = busy();
  // A selection must be made against the exact file OCR will crop.  Refresh
  // this state immediately when someone switches between source and final.
  renderAnnotationControls();
}

async function loadSubtitles() {
  const data = await api(projectUrl('/subtitles'));
  rows = data.rows;
  revision = data.revision;
  setDirty(false);
  page = 0;
  renderRows();
  $('editor-status').textContent = pipelineBlockedBySource()
    ? sourceError() ? 'Cần tạo lại video tua chậm' : 'Chờ duyệt video tua chậm'
    : data.vietnamese ? 'Đang sửa bản Việt' : 'Tự dịch từ phụ đề Trung';
  void preloadChineseSrt();
  void preloadAnnotationChineseSrt();
}

async function preloadChineseSrt() {
  const projectId = active?.id;
  chineseSrt = '';
  chineseSrtProject = null;
  updateFiles();
  if (!projectId || !hasFile('zh')) return;
  try {
    const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/files/zh`);
    if (!response.ok) throw new Error('Không đọc được SRT Trung.');
    const text = await response.text();
    if (active?.id === projectId) {
      chineseSrt = text;
      chineseSrtProject = projectId;
      updateFiles();
    }
  } catch (_) {
    if (active?.id === projectId) updateFiles();
  }
}

async function preloadAnnotationChineseSrt() {
  const projectId = active?.id;
  annotationChineseSrt = '';
  annotationChineseSrtProject = null;
  renderAnnotationControls();
  if (!projectId || !hasFile('annotation_zh')) return;
  try {
    const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/files/annotation_zh`);
    if (!response.ok) throw new Error('Không đọc được SRT Trung của vùng khoanh.');
    const text = await response.text();
    if (active?.id === projectId) {
      annotationChineseSrt = text;
      annotationChineseSrtProject = projectId;
      renderAnnotationControls();
    }
  } catch (_) {
    if (active?.id === projectId) renderAnnotationControls();
  }
}

async function copyChineseSrt() {
  if (!hasFile('zh')) throw new Error('Chưa có SRT Trung. Hãy chạy OCR trước.');
  if (chineseSrtProject !== active.id) throw new Error('SRT Trung đang tải, hãy thử lại sau một lát.');
  await copyText(chineseSrt);
  toast('Đã chép SRT Trung vào bộ nhớ tạm.');
}

async function copyAnnotationChineseSrt() {
  if (!hasFile('annotation_zh')) {
    throw new Error('Chưa có SRT Trung của vùng khoanh. Hãy OCR vùng này trước.');
  }
  if (annotationChineseSrtProject !== active?.id) {
    throw new Error('SRT Trung của vùng khoanh đang tải. Hãy thử lại sau một lát.');
  }
  if (!annotationChineseSrt.trim()) throw new Error('SRT Trung của vùng khoanh đang trống.');
  await copyText(annotationChineseSrt);
  toast('Đã chép SRT Trung của vùng khoanh vào bộ nhớ tạm.');
}

function renderRows() {
  const query = $('search').value.toLocaleLowerCase();
  const filtered = rows.filter(row => `${row.zh} ${row.text}`.toLocaleLowerCase().includes(query));
  const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
  page = Math.min(page, pages - 1);
  $('cue-count').textContent = `${rows.length} câu`;
  $('subtitle-rows').replaceChildren();
  $('no-subtitles').hidden = filtered.length > 0;
  for (const row of filtered.slice(page * pageSize, (page + 1) * pageSize)) {
    const tr = document.createElement('tr');
    const id = document.createElement('td');
    id.textContent = row.id;
    tr.append(id);
    const timing = document.createElement('td');
    const seek = document.createElement('button');
    seek.className = 'timestamp';
    seek.title = 'Xem video tại thời điểm này';
    seek.textContent = timeText(row.start);
    const end = document.createElement('span');
    end.textContent = `→ ${timeText(row.end)}`;
    seek.append(end);
    seek.onclick = () => {
      $('video').currentTime = row.start;
      $('video').play().catch(() => {});
    };
    timing.append(seek);
    tr.append(timing);
    const zh = document.createElement('td');
    zh.textContent = row.zh;
    tr.append(zh);
    const vi = document.createElement('td');
    const text = document.createElement('textarea');
    text.value = row.text;
    text.rows = 2;
    text.placeholder = 'Nhập lời Việt…';
    text.setAttribute('aria-label', `Lời Việt câu ${row.id}`);
    text.disabled = busy() || pipelineBlockedBySource();
    text.oninput = () => {
      row.text = text.value;
      setDirty(true);
    };
    vi.append(text);
    tr.append(vi);
    const listen = document.createElement('td');
    const button = document.createElement('button');
    button.className = 'listen-button';
    button.textContent = '▷';
    button.title = 'Nghe WAV đã tạo (tốc độ gốc)';
    button.onclick = () => {
      $('audio-player').src = projectUrl(`/audio/${row.id}`);
      $('audio-player').play().catch(() => toast('Câu này chưa có audio, hoặc đã gộp vào câu trước.', true));
    };
    listen.append(button);
    tr.append(listen);
    $('subtitle-rows').append(tr);
  }
  $('page-label').textContent = `${filtered.length} mục · Trang ${page + 1} / ${pages}`;
  $('prev-page').disabled = page === 0;
  $('next-page').disabled = page >= pages - 1;
}

async function saveSubtitles() {
  if (pipelineBlockedBySource()) throw new Error(sourceError() || 'Hãy duyệt video tua chậm trước khi sửa hoặc lưu phụ đề.');
  const data = await api(projectUrl('/subtitles'), {rows, revision});
  revision = data.revision;
  setDirty(false);
  await loadSubtitles();
  await refreshProjects();
  toast(`Đã lưu ${data.saved} câu. Bản trước được sao lưu nếu đã tồn tại.`);
}

async function saveOptions() {
  const options = {};
  for (const key of optionNames) {
    options[key] = key === 'tts_pitch' ? ttsPitchSetting()
      : booleans.has(key) ? $(key).checked : strings.has(key) ? $(key).value : Number($(key).value);
  }
  const data = await api(projectUrl('/options'), options);
  active = data.project;
}

async function chooseSourceMode(mode) {
  if (busy()) throw new Error('Có tác vụ đang chạy.');
  if (dirty) throw new Error('Hãy lưu phụ đề đang sửa trước khi đổi video đầu vào.');
  if (mode === 'slow') {
    if (!hasReadySlow()) throw new Error('Chưa có video tua chậm hợp lệ để duyệt. Hãy tạo lại video.');
    if (!await confirmAction('Dùng video tua chậm?', 'OCR sẽ lấy mốc thời gian từ bản này. Phụ đề, giọng Việt và video xuất sau đó sẽ dùng cùng timeline tua chậm.')) return;
  } else {
    const stale = Boolean(sourceError());
    const title = stale ? 'Bỏ bản tua chậm cũ?' : 'Dùng video gốc?';
    const message = stale
      ? 'Bản tua chậm cũ không còn hợp lệ và sẽ được lưu ngoài pipeline. Sau đó bạn có thể tạo bản chậm mới.'
      : 'Bạn sẽ tiếp tục theo timeline video gốc. Bản tua chậm chờ duyệt sẽ được bỏ khỏi pipeline; muốn dùng lại, hãy tạo bản mới để xem và duyệt.';
    if (!await confirmAction(title, message)) return;
  }
  await api(projectUrl('/source-mode'), {mode});
  await refreshProjects();
  showVideo(mode === 'slow' ? 'slow' : 'original');
  renderJob();
  toast(mode === 'slow' ? 'Đã duyệt video tua chậm. Bạn có thể bắt đầu OCR.' : 'Đã chuyển về video gốc.');
}

async function startAction(action) {
  if (busy()) throw new Error('Có tác vụ đang chạy.');
  if (dirty) throw new Error('Hãy lưu phụ đề đang sửa trước khi chạy.');
  if (['dub', 'mix', 'all', 'annotation_burn'].includes(action) && $('burn_logo').checked && !hasFile('logo')) {
    throw new Error('Hãy chọn logo PNG, JPG hoặc WebP trước khi xuất video.');
  }
  if (['annotation_ocr', 'annotation_ocr_enhanced', 'annotation_translate', 'annotation_burn'].includes(action) && !$('annotation_ocr_enabled').checked) {
    throw new Error('Hãy khoanh vùng vị trí chú thích trực tiếp trên video trước.');
  }
  if (action !== 'slow' && pipelineBlockedBySource()) {
    throw new Error(sourceError() || 'Hãy xem và chọn dùng video tua chậm trước khi chạy bước tiếp theo.');
  }
  let slowSpeed = null;
  if (action === 'slow') {
    if (sourceState() === 'approved') throw new Error('Bản tua chậm đã duyệt đang được dùng. Hãy chọn “Dùng lại video gốc” trước khi tạo bản mới.');
    slowSpeed = selectedSlowSpeed();
    if (!(slowSpeed >= 0.5 && slowSpeed <= 0.95)) throw new Error('Tốc độ video Trung phải từ 0.50× đến 0.95×.');
    if (hasReadySlow() && !await confirmAction('Tạo lại video tua chậm?', 'Bản xem hiện có sẽ được thay bằng video mới. Bạn sẽ cần xem và duyệt lại trước khi tiếp tục.')) return;
  }
  if (action === 'translate' && hasFile('vi')) {
    toast('Đã có bản Việt. Bạn có thể sửa trực tiếp; Gemini sẽ giữ nguyên file này.');
    return;
  }
  if (action === 'annotation_translate' && hasFile('annotation_vi')) {
    toast(annotationManualImportNotice());
    return;
  }
  if (action === 'translate' && !await confirmAction('Dịch bằng Gemini?', 'Văn bản phụ đề sẽ gửi đến Gemini bằng key trong ô nhập hoặc .env. Tài khoản có thể bị tính quota/phí.')) return;
  if (action === 'annotation_translate' && !await confirmAction('Dịch chú thích bằng Gemini?', 'Chỉ văn bản OCR trong vùng bạn đã khoanh sẽ gửi đến Gemini. Bản dịch được dùng để chèn vào đúng vị trí trên video và không tạo giọng đọc.')) return;
  if (action === 'ocr' && $('reocr').checked && !await confirmAction('OCR lại video?', 'SRT Trung hiện có sẽ được sao lưu rồi nhận dạng lại. Bản Việt đang sửa được giữ nguyên.')) return;
  let annotationReocr = action === 'annotation_ocr_enhanced';
  if (['annotation_ocr', 'annotation_ocr_enhanced'].includes(action) && hasFile('annotation_zh')) {
    const enhanced = action === 'annotation_ocr_enhanced';
    const title = enhanced ? 'OCR tăng cường lại vùng chú thích?' : 'OCR lại vùng chú thích?';
    const message = enhanced
      ? 'SRT chú thích Trung cũ sẽ được sao lưu. PP-OCRv5 Server đọc chữ dọc/cách điệu chậm hơn; SRT Việt cũ được giữ nguyên để bạn kiểm tra.'
      : 'SRT chú thích Trung cũ sẽ được sao lưu. Bản Việt cũ được giữ nguyên để bạn kiểm tra.';
    if (!await confirmAction(title, message)) return;
    annotationReocr = true;
  }
  await saveOptions();
  const key = $('api-key').value;
  $('api-key').value = '';
  const request = {
    action,
    burn: $('burn').checked,
    reocr: (action === 'ocr' && $('reocr').checked) || annotationReocr,
    api_key: ['translate', 'annotation_translate'].includes(action) ? key : '',
  };
  if (action === 'slow') request.slow_speed = slowSpeed;
  const response = await api(projectUrl('/jobs'), request);
  job = response.job;
  lastJobId = job.id;
  lastJobStatus = job.status;
  cursor = 0;
  $('job-log').textContent = '';
  renderJob();
  toast(action === 'slow' ? 'Đang tạo video tua chậm để xem.' : 'Đã bắt đầu tác vụ. Bạn có thể theo dõi tiến trình bên dưới.');
}

function renderJob() {
  const statuses = {running: 'Đang chạy', stopping: 'Đang dừng', completed: 'Hoàn tất', failed: 'Có lỗi', cancelled: 'Đã dừng'};
  $('job-status').textContent = job ? statuses[job.status] : 'Sẵn sàng';
  $('job-stage').textContent = job
    ? `${projects.find(project => project.id === job.project)?.name || 'Video'} · ${job.stage}`
    : 'Mỗi lần chạy một tác vụ để giữ dữ liệu nhất quán.';
  if (busy() && job.progress === null) $('job-progress').removeAttribute('value');
  else $('job-progress').value = job?.status === 'completed' ? 100 : (job?.progress || 0);
  $('cancel-job').disabled = !busy() || job.status === 'stopping';
  const pipelineLocked = busy() || pipelineBlockedBySource();
  for (const id of ['export-video', 'save-srt', 'ocr', 'translate', 'annotation-ocr', 'annotation-ocr-enhanced', 'annotation-translate', 'annotation-burn', 'draw-annotation', 'clear-annotation', 'preview-cleanup', 'execute-cleanup', 'import-srt']) {
    $(id).disabled = pipelineLocked;
  }
  $('tts_voice').disabled = busy();
  $('tts_pitch').disabled = busy();
  $('preview-tts-voice').disabled = busy() || !selectedEdgeVoice();
  $('save-final').disabled = busy();
  $('copy-zh-srt').disabled = busy() || chineseSrtProject !== active?.id || !chineseSrt.trim();
  document.querySelectorAll('[data-action], #subtitle-rows textarea').forEach(element => {
    element.disabled = pipelineLocked;
  });
  renderSlowWorkflow();
  renderVoiceCard();
  renderLogoControls();
  renderAnnotationControls();
}

async function poll() {
  if (polling) return;
  polling = true;
  try {
    const data = await api(`/api/job?after=${cursor}`);
    if (data.job && lastJobId !== data.job.id) {
      cursor = 0;
      $('job-log').textContent = '';
      lastJobId = data.job.id;
      lastJobStatus = null;
      polling = false;
      return;
    }
    job = data.job;
    cursor = data.cursor;
    for (const line of data.logs) $('job-log').textContent += `${line.text}\n`;
    if (data.logs.length) {
      const lines = $('job-log').textContent.split('\n');
      if (lines.length > 1000) $('job-log').textContent = lines.slice(-1000).join('\n');
      $('job-log').scrollTop = $('job-log').scrollHeight;
    }
    renderJob();
    if (job && job.status !== lastJobStatus) {
      lastJobStatus = job.status;
      if (!busy()) {
        await refreshProjects();
        if (active?.id === job.project) {
          if (job.status === 'completed' && job.action === 'slow') showVideo('slow');
          else if (!dirty) await loadSubtitles();
        }
        const completedSlow = job.status === 'completed' && job.action === 'slow';
        const completedVoice = job.status === 'completed' && job.action === 'voice';
        toast(
          completedSlow ? 'Đã tạo video tua chậm. Hãy xem và duyệt trước khi OCR.'
            : completedVoice ? 'Đã đổi giọng RVC. Bạn có thể nghe hoặc tải WAV/MP4 ở thẻ Đổi giọng.'
            : job.status === 'completed' ? 'Tác vụ hoàn tất.'
              : job.status === 'cancelled' ? 'Đã dừng. Các file hoàn thành được giữ lại.'
                : 'Tác vụ gặp lỗi. Xem dòng cuối trong tiến trình.',
          job.status === 'failed',
        );
      }
    }
  } catch (_) {
    $('job-stage').textContent = 'Mất kết nối app. Kiểm tra terminal chạy web_app.py.';
  } finally {
    polling = false;
  }
}

function switchTab(tab) {
  $('studio-tab').hidden = tab !== 'studio';
  $('storage-tab').hidden = tab !== 'storage';
  document.querySelectorAll('.nav').forEach(button => button.classList.toggle('active', button.dataset.tab === tab));
  $('breadcrumb').textContent = `Không gian làm việc / ${tab === 'studio' ? 'Xưởng video' : 'Dọn dẹp dữ liệu'}`;
}

function invalidatePreview() {
  preview = null;
  $('cleanup-preview').hidden = true;
}

async function previewCleanup() {
  const categories = [...document.querySelectorAll('input[name=cleanup]:checked')].map(input => input.value);
  preview = await api(projectUrl('/cleanup/preview'), {categories});
  $('cleanup-preview').hidden = false;
  $('cleanup-size').textContent = `${sizeText(preview.bytes)} · ${preview.count} file`;
  $('execute-cleanup').disabled = !preview.count;
  $('cleanup-files').replaceChildren();
  for (const file of preview.files) {
    const item = document.createElement('li');
    item.textContent = `${file.path} · ${sizeText(file.size)}`;
    $('cleanup-files').append(item);
  }
}

async function executeCleanup() {
  if (!preview) throw new Error('Hãy xem trước trước khi xóa.');
  if (!await confirmAction('Xóa dữ liệu đã chọn?', `Xóa vĩnh viễn ${preview.count} file (${sizeText(preview.bytes)}) đã liệt kê. Video gốc và SRT đang sửa được giữ lại.`)) return;
  const result = await api(projectUrl('/cleanup/execute'), {token: preview.token});
  invalidatePreview();
  await refreshProjects();
  toast(`Đã xóa ${result.removed} file.${result.errors.length ? ` Một số file đang bị giữ: ${result.errors.join(', ')}` : ''}`, result.errors.length > 0);
}

function openAdd() {
  $('project-dialog').showModal();
  $('video-path').focus();
}

for (const id of ['new-project', 'add-video', 'empty-add']) $(id).onclick = openAdd;
$('close-dialog').onclick = () => $('project-dialog').close();
$('project-form').onsubmit = handle(async event => {
  event.preventDefault();
  const data = await api('/api/projects', {path: $('video-path').value, legacy: $('legacy').checked});
  $('project-dialog').close();
  await refreshProjects();
  await selectProject(data.project.id);
  toast('Đã mở video.');
});
$('upload-video').onchange = handle(async () => {
  const file = $('upload-video').files[0];
  if (!file) return;
  if (dirty) throw new Error('Lưu phụ đề trước khi thêm video.');
  const form = new FormData();
  form.append('video', file);
  $('upload-progress').hidden = false;
  await new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload');
    xhr.setRequestHeader('X-CSRF-Token', csrf);
    xhr.upload.onprogress = event => {
      if (event.lengthComputable) $('upload-progress').value = 100 * event.loaded / event.total;
    };
    xhr.onerror = () => reject(new Error('Tải video thất bại.'));
    xhr.onload = async () => {
      try {
        const data = JSON.parse(xhr.responseText);
        if (xhr.status >= 400) throw new Error(data.error);
        $('project-dialog').close();
        await refreshProjects();
        await selectProject(data.project.id);
        resolve();
      } catch (error) {
        reject(error);
      } finally {
        $('upload-progress').hidden = true;
      }
    };
    xhr.send(form);
  });
});

$('voice_speed').oninput = updateSpeed;
$('slow_speed').oninput = updateSlowSpeed;
$('tts_pitch').oninput = updateTtsPitch;
$('tts_pitch').onchange = handle(saveOptions);
$('tts_voice').onchange = handle(applyEdgeVoice);
$('preview-tts-voice').onclick = handle(previewTtsVoice);
for (const id of ['logo_width_percent', 'logo_opacity', 'logo_margin_percent']) $(id).oninput = updateLogoLabels;
$('annotation_font_size').oninput = () => {
  $('annotation-font-size-label').textContent = String(Math.round(Number($('annotation_font_size').value) || 28));
};
$('annotation_font_size').onchange = handle(saveOptions);
$('annotation_display_y').oninput = updateAnnotationDisplayY;
$('annotation_display_y').onchange = handle(saveOptions);
$('burn_logo').onchange = renderLogoControls;
$('logo-upload').onchange = handle(uploadLogo);
$('draw-annotation').onclick = handle(beginAnnotationDraw);
$('clear-annotation').onclick = handle(clearAnnotationRegion);
$('import-annotation-srt').onchange = handle(importAnnotationSrt);
$('annotation-ocr').onclick = handle(() => startAction('annotation_ocr'));
$('annotation-ocr-enhanced').onclick = handle(() => startAction('annotation_ocr_enhanced'));
$('annotation-translate').onclick = handle(() => startAction('annotation_translate'));
$('annotation-burn').onclick = handle(() => startAction('annotation_burn'));
$('annotation_ocr_enabled').onchange = handle(async () => {
  await saveOptions();
  await refreshProjects();
  renderAnnotationControls();
});
const annotationOverlay = $('annotation-overlay');
annotationOverlay.addEventListener('pointerdown', event => {
  if (!annotationDrawMode) return;
  const point = regionPointFromPointer(event);
  if (!point) return;
  event.preventDefault();
  annotationStart = point;
  annotationOverlay.setPointerCapture(event.pointerId);
  renderAnnotationRegionBox(regionBetween(point, point), true);
});
annotationOverlay.addEventListener('pointermove', event => {
  if (!annotationDrawMode || !annotationStart) return;
  const point = regionPointFromPointer(event, true);
  if (point) renderAnnotationRegionBox(regionBetween(annotationStart, point), true);
});
annotationOverlay.addEventListener('pointerup', event => {
  if (annotationOverlay.hasPointerCapture(event.pointerId)) annotationOverlay.releasePointerCapture(event.pointerId);
  void handle(finishAnnotationDraw)(event);
});
annotationOverlay.addEventListener('pointercancel', () => {
  endAnnotationDraw();
  renderAnnotationControls();
});
$('video').addEventListener('loadedmetadata', () => renderAnnotationControls());
window.addEventListener('resize', () => renderAnnotationRegionBox());
$('voice_pitch').oninput = updateVoicePitchLabels;
$('voice_post_pitch').oninput = updateVoicePitchLabels;
for (const id of ['voice_source_mode', 'voice_model', 'voice_preset', 'voice_speaker_id', 'voice_pitch', 'voice_post_pitch']) {
  $(id).onchange = handle(saveVoiceOptionsAfterPreviewChange);
}
$('voice-upload').onchange = handle(uploadVoiceSource);
$('preview-rvc-model').onclick = handle(previewRvcModel);
$('convert-voice').onclick = handle(startVoiceConversion);
$('show-original').onclick = () => showVideo('original');
$('show-slow').onclick = () => showVideo('slow');
$('show-final').onclick = () => showVideo('final');
$('create-slow').onclick = handle(() => startAction('slow'));
$('approve-slow').onclick = handle(() => chooseSourceMode('slow'));
$('use-original').onclick = handle(() => chooseSourceMode('original'));
$('return-original').onclick = handle(() => chooseSourceMode('original'));
$('save-srt').onclick = handle(saveSubtitles);
$('ocr').onclick = handle(() => startAction('ocr'));
$('translate').onclick = handle(() => startAction('translate'));
$('export-video').onclick = handle(() => startAction('dub'));
$('save-final').onclick = handle(async () => {
  const button = $('save-final');
  button.disabled = true;
  try {
    const result = await api(projectUrl('/save-final-dialog'), {});
    if (!result.cancelled) toast(`Đã lưu ${result.name} tại ${result.path}`);
  } finally {
    button.disabled = busy();
  }
});
$('copy-zh-srt').onclick = handle(copyChineseSrt);
const copyAnnotationButton = annotationCopyButton();
if (copyAnnotationButton) copyAnnotationButton.onclick = handle(copyAnnotationChineseSrt);
document.querySelectorAll('[data-action]').forEach(element => {
  element.onclick = handle(() => startAction(element.dataset.action));
});
$('cancel-job').onclick = handle(async () => {
  await api('/api/job/cancel', {});
  await poll();
});
$('search').oninput = () => {
  page = 0;
  renderRows();
};
$('prev-page').onclick = () => {
  page -= 1;
  renderRows();
};
$('next-page').onclick = () => {
  page += 1;
  renderRows();
};
$('reload-srt').onclick = handle(async () => {
  if (!dirty || await confirmAction('Tải lại phụ đề?', 'Bỏ các thay đổi chưa lưu và đọc lại file trên máy.')) await loadSubtitles();
});
$('import-srt').onchange = handle(async () => {
  const file = $('import-srt').files[0];
  if (!file) return;
  if (pipelineBlockedBySource()) throw new Error(sourceError() || 'Hãy duyệt video tua chậm trước khi nhập phụ đề.');
  if (!await confirmAction('Nhập bản SRT Việt?', 'Bản nhập sẽ thay nội dung trong trình sửa. Khi lưu, bản Việt hiện có được sao lưu.')) return;
  const content = await file.text();
  await api(projectUrl('/import-srt'), {content, revision});
  await loadSubtitles();
  await refreshProjects();
  $('import-srt').value = '';
  toast('Đã nhập và lưu SRT Việt.');
});
document.querySelectorAll('.nav').forEach(element => {
  element.onclick = () => switchTab(element.dataset.tab);
});
document.querySelectorAll('input[name=cleanup]').forEach(element => {
  element.onchange = invalidatePreview;
});
$('preview-cleanup').onclick = handle(previewCleanup);
$('execute-cleanup').onclick = handle(executeCleanup);
window.addEventListener('beforeunload', event => {
  if (dirty) {
    event.preventDefault();
    event.returnValue = '';
  }
});

(async () => {
  try {
    await refreshProjects();
    const selected = localStorage.getItem('vietdub-project');
    if (projects.length) await selectProject(projects.find(project => project.id === selected)?.id || projects[0].id);
    await poll();
  } catch (error) {
    toast(error.message, true);
  }
  setInterval(poll, 1500);
})();
