from pathlib import Path
import re, sys
from xml.sax.saxutils import escape

root = Path(sys.argv[1] if len(sys.argv) > 1 else '.')

def read(rel): return (root/rel).read_text(encoding='utf-8')
def write(rel, text):
    p=root/rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text, encoding='utf-8')
def must_replace(text, old, new, label):
    if old not in text:
        raise RuntimeError(f'pattern not found for {label}')
    return text.replace(old,new,1)

p='app/build.gradle.kts'; s=read(p)
s=must_replace(s,'versionCode = 2','versionCode = 3','versionCode')
s=must_replace(s,'versionName = "0.2.0"','versionName = "0.2.1-custom60-tw-attachments"','versionName')
needle='    implementation(libs.okhttp)\n'
if 'com.tom-roush:pdfbox-android' not in s:
    s=must_replace(s,needle,needle+'    implementation("com.tom-roush:pdfbox-android:2.0.27.0")\n','pdfbox dependency')
write(p,s)

p='app/src/main/kotlin/com/phoneai/agent/agent/AgentLoop.kt'; s=read(p)
s=s.replace('private val maxIterations: Int = 15','private val maxIterations: Int = 60')
old='''    suspend fun run(userMessage: String, history: List<ChatMessage> = emptyList(), onEvent: (AgentEvent) -> Unit) {
        onEvent(AgentEvent.UserRequest(userMessage))'''
new='''    suspend fun run(userMessage: String, history: List<ChatMessage> = emptyList(), onEvent: (AgentEvent) -> Unit) =
        runInternal(userMessage, history, emptyList(), onEvent)

    suspend fun runWithImages(
        userMessage: String,
        history: List<ChatMessage> = emptyList(),
        images: List<ChatImage>,
        onEvent: (AgentEvent) -> Unit
    ) = runInternal(userMessage, history, images, onEvent)

    private suspend fun runInternal(
        userMessage: String,
        history: List<ChatMessage>,
        initialImages: List<ChatImage>,
        onEvent: (AgentEvent) -> Unit
    ) {
        onEvent(AgentEvent.UserRequest(userMessage))'''
s=must_replace(s,old,new,'AgentLoop overload')
s=must_replace(s,
    'messages += ChatMessage(role = ChatRole.user, content = userMessage)',
    'messages += ChatMessage(role = ChatRole.user, content = userMessage, images = initialImages)',
    'initial images')
write(p,s)

p='app/src/main/kotlin/com/phoneai/agent/chat/DefaultAgentLoopFactory.kt'; s=read(p)
s=s.replace('else 15,','else 60,')
write(p,s)

attachment_reader = r'''package com.phoneai.agent.attachment

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import android.text.Html
import android.util.Base64
import com.tom_roush.pdfbox.android.PDFBoxResourceLoader
import com.tom_roush.pdfbox.pdmodel.PDDocument
import com.tom_roush.pdfbox.text.PDFTextStripper
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.ByteArrayOutputStream
import java.util.Locale
import java.util.zip.ZipInputStream

data class PendingAttachment(
    val name: String,
    val mimeType: String,
    val textContent: String? = null,
    val imageBase64: String? = null,
    val truncated: Boolean = false
)

object AttachmentReader {
    private const val MAX_TEXT_CHARS = 50_000
    private const val MAX_IMAGE_BYTES = 8 * 1024 * 1024
    private const val MAX_GENERIC_BYTES = 8 * 1024 * 1024

    suspend fun read(context: Context, uri: Uri): PendingAttachment = withContext(Dispatchers.IO) {
        val resolver = context.contentResolver
        val name = displayName(context, uri)
        val mime = resolver.getType(uri) ?: guessMime(name)
        val lower = name.lowercase(Locale.ROOT)
        when {
            mime.startsWith("image/") || lower.endsWith(".png") || lower.endsWith(".jpg") ||
                lower.endsWith(".jpeg") || lower.endsWith(".webp") -> {
                val bytes = resolver.openInputStream(uri)?.use { readLimited(it, MAX_IMAGE_BYTES) }
                    ?: error("無法讀取圖片")
                PendingAttachment(name, mime.ifBlank { "image/jpeg" }, imageBase64 = Base64.encodeToString(bytes, Base64.NO_WRAP))
            }
            mime == "application/pdf" || lower.endsWith(".pdf") -> {
                PDFBoxResourceLoader.init(context.applicationContext)
                val text = resolver.openInputStream(uri)?.use { input ->
                    PDDocument.load(input).use { doc -> PDFTextStripper().getText(doc) }
                } ?: error("無法讀取 PDF")
                val (cut, truncated) = limitText(text)
                PendingAttachment(name, "application/pdf", textContent = cut, truncated = truncated)
            }
            lower.endsWith(".docx") || mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" -> {
                val entries = resolver.openInputStream(uri)?.use { readZipTextEntries(it, setOf("word/document.xml")) }
                    ?: error("無法讀取 DOCX")
                val xml = entries["word/document.xml"] ?: error("DOCX 缺少 document.xml")
                val text = xmlToPlain(xml
                    .replace("</w:p>", "\n")
                    .replace("</w:tr>", "\n")
                    .replace("</w:tc>", "\t"))
                val (cut, truncated) = limitText(text)
                PendingAttachment(name, mime, textContent = cut, truncated = truncated)
            }
            lower.endsWith(".xlsx") || mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" -> {
                val entries = resolver.openInputStream(uri)?.use { readZipTextEntries(it, null) }
                    ?: error("無法讀取 XLSX")
                val text = extractXlsx(entries)
                val (cut, truncated) = limitText(text)
                PendingAttachment(name, mime, textContent = cut, truncated = truncated)
            }
            else -> {
                val bytes = resolver.openInputStream(uri)?.use { readLimited(it, MAX_GENERIC_BYTES) }
                    ?: error("無法讀取檔案")
                if (bytes.any { it == 0.toByte() }) error("目前無法解析此二進位檔案格式")
                val text = bytes.toString(Charsets.UTF_8)
                val (cut, truncated) = limitText(text)
                PendingAttachment(name, mime.ifBlank { "text/plain" }, textContent = cut, truncated = truncated)
            }
        }
    }

    private fun displayName(context: Context, uri: Uri): String {
        context.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
            if (c.moveToFirst()) {
                val i = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if (i >= 0) return c.getString(i) ?: "附件"
            }
        }
        return uri.lastPathSegment ?: "附件"
    }

    private fun guessMime(name: String): String = when (name.substringAfterLast('.', "").lowercase(Locale.ROOT)) {
        "png" -> "image/png"; "jpg", "jpeg" -> "image/jpeg"; "webp" -> "image/webp"
        "pdf" -> "application/pdf"; "docx" -> "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        "xlsx" -> "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        "json" -> "application/json"; "csv" -> "text/csv"; "md", "txt" -> "text/plain"
        else -> "application/octet-stream"
    }

    private fun readLimited(input: java.io.InputStream, max: Int): ByteArray {
        val out = ByteArrayOutputStream()
        val buf = ByteArray(16 * 1024)
        var total = 0
        while (true) {
            val n = input.read(buf)
            if (n <= 0) break
            total += n
            if (total > max) error("檔案過大，目前上限為 ${max / 1024 / 1024} MB")
            out.write(buf, 0, n)
        }
        return out.toByteArray()
    }

    private fun readZipTextEntries(input: java.io.InputStream, wanted: Set<String>?): Map<String, String> {
        val out = linkedMapOf<String, String>()
        ZipInputStream(input).use { zip ->
            while (true) {
                val e = zip.nextEntry ?: break
                val take = !e.isDirectory && (wanted == null && (e.name == "xl/sharedStrings.xml" || e.name.startsWith("xl/worksheets/sheet")) || wanted?.contains(e.name) == true)
                if (take) {
                    val bytes = readLimited(zip, MAX_GENERIC_BYTES)
                    out[e.name] = bytes.toString(Charsets.UTF_8)
                }
                zip.closeEntry()
            }
        }
        return out
    }

    private fun extractXlsx(entries: Map<String, String>): String {
        val sharedXml = entries["xl/sharedStrings.xml"].orEmpty()
        val shared = Regex("<t[^>]*>(.*?)</t>", setOf(RegexOption.DOT_MATCHES_ALL, RegexOption.IGNORE_CASE))
            .findAll(sharedXml).map { xmlDecode(it.groupValues[1]) }.toList()
        val sb = StringBuilder()
        entries.filterKeys { it.startsWith("xl/worksheets/sheet") }.toSortedMap().forEach { (name, xml) ->
            sb.append("\n[").append(name.substringAfterLast('/')).append("]\n")
            val rows = Regex("<row[^>]*>(.*?)</row>", setOf(RegexOption.DOT_MATCHES_ALL, RegexOption.IGNORE_CASE))
            for (row in rows.findAll(xml)) {
                val cells = Regex("<c([^>]*)>(.*?)</c>", setOf(RegexOption.DOT_MATCHES_ALL, RegexOption.IGNORE_CASE))
                val vals = cells.findAll(row.groupValues[1]).map { cell ->
                    val attrs = cell.groupValues[1]
                    val body = cell.groupValues[2]
                    val v = Regex("<v[^>]*>(.*?)</v>", setOf(RegexOption.DOT_MATCHES_ALL, RegexOption.IGNORE_CASE)).find(body)?.groupValues?.get(1)
                        ?: Regex("<t[^>]*>(.*?)</t>", setOf(RegexOption.DOT_MATCHES_ALL, RegexOption.IGNORE_CASE)).find(body)?.groupValues?.get(1).orEmpty()
                    if (Regex("\\bt=\"s\"").containsMatchIn(attrs)) shared.getOrNull(v.toIntOrNull() ?: -1).orEmpty() else xmlDecode(v)
                }.toList()
                if (vals.isNotEmpty()) sb.append(vals.joinToString("\t")).append('\n')
                if (sb.length > MAX_TEXT_CHARS * 2) break
            }
        }
        return sb.toString().trim()
    }

    private fun xmlToPlain(xml: String): String {
        val noTags = xml.replace(Regex("<[^>]+>"), "")
        return xmlDecode(noTags).replace(Regex("[ \\t]+"), " ").replace(Regex("\\n{3,}"), "\n\n").trim()
    }

    private fun xmlDecode(s: String): String = Html.fromHtml(s, Html.FROM_HTML_MODE_LEGACY).toString()

    private fun limitText(text: String): Pair<String, Boolean> {
        val clean = text.trim()
        return if (clean.length <= MAX_TEXT_CHARS) clean to false
        else (clean.take(MAX_TEXT_CHARS) + "\n\n[附件內容過長，已截斷至前 $MAX_TEXT_CHARS 個字元]") to true
    }
}
'''
write('app/src/main/kotlin/com/phoneai/agent/attachment/AttachmentReader.kt', attachment_reader)

p='app/src/main/kotlin/com/phoneai/agent/chat/ChatViewModel.kt'; s=read(p)
s=s.replace('import com.phoneai.agent.agent.AgentLoop\n', 'import com.phoneai.agent.agent.AgentLoop\nimport com.phoneai.agent.attachment.PendingAttachment\n')
s=s.replace('import com.phoneai.agent.overlay.NoopOverlayController\n', 'import com.phoneai.agent.overlay.NoopOverlayController\nimport com.phoneai.agent.network.ChatImage\n')
s=s.replace('"新会话"','"新對話"').replace('"未配置模型提供商，请先在设置中添加。"','"尚未設定模型提供者，請先到設定中新增。"')
old='''    fun send(text: String = _ui.value.input) {
        if (text.isBlank() || _ui.value.isRunning) return
        viewModelScope.launch {
            val convId = ensureConversation()
            // Load prior history BEFORE persisting the new user message, so the history passed
            // to the loop excludes the current turn (the loop appends it itself).
            val history = HistoryLoader.load(conversationRepo.getMessages(convId))
            val userRowId = conversationRepo.addMessage(convId, MessageKind.USER, text)
            // First user message becomes the conversation title (shown in the history drawer).
            if (conversationRepo.messageCount(convId) == 1) {
                conversationRepo.updateTitle(convId, text.take(40))
            }
            _ui.update {
                it.copy(
                    messages = it.messages + ChatMessageItem.User(nextId++, text, dbId = userRowId),
                    isRunning = true,
                    input = ""
                )
            }
            val loop: AgentLoop? = loopFactory.create(executor)'''
new='''    fun send(text: String = _ui.value.input) = sendWithAttachments(text, emptyList())

    fun sendWithAttachments(attachments: List<PendingAttachment>) =
        sendWithAttachments(_ui.value.input, attachments)

    private fun sendWithAttachments(text: String, attachments: List<PendingAttachment>) {
        if ((text.isBlank() && attachments.isEmpty()) || _ui.value.isRunning) return
        viewModelScope.launch {
            val convId = ensureConversation()
            val displayText = buildDisplayText(text, attachments)
            val modelText = buildModelText(text, attachments)
            // Load prior history BEFORE persisting the new user message, so the history passed
            // to the loop excludes the current turn (the loop appends it itself).
            val history = HistoryLoader.load(conversationRepo.getMessages(convId))
            val userRowId = conversationRepo.addMessage(convId, MessageKind.USER, modelText)
            // First user message becomes the conversation title (shown in the history drawer).
            if (conversationRepo.messageCount(convId) == 1) {
                conversationRepo.updateTitle(convId, displayText.take(40))
            }
            _ui.update {
                it.copy(
                    messages = it.messages + ChatMessageItem.User(nextId++, displayText, dbId = userRowId),
                    isRunning = true,
                    input = ""
                )
            }
            val loop: AgentLoop? = loopFactory.create(executor)'''
s=must_replace(s,old,new,'ChatViewModel send header')
old='''            runJob = launch {
                try {
                    loop.run(text, history) { event -> handleEvent(event) }
                } finally {'''
new='''            val images = attachments.mapNotNull { a ->
                a.imageBase64?.let { ChatImage(base64 = it, mimeType = a.mimeType) }
            }
            runJob = launch {
                try {
                    if (images.isEmpty()) {
                        loop.run(modelText, history) { event -> handleEvent(event) }
                    } else {
                        loop.runWithImages(modelText, history, images) { event -> handleEvent(event) }
                    }
                } finally {'''
s=must_replace(s,old,new,'ChatViewModel run images')
s=must_replace(s,
    'MessageKind.USER.name -> ChatMessageItem.User(uiId, content, dbId = id)',
    'MessageKind.USER.name -> ChatMessageItem.User(uiId, displayPersistedUserContent(content), dbId = id)',
    'display persisted attachment')
helper='''

    private fun buildDisplayText(text: String, attachments: List<PendingAttachment>): String {
        val base = text.trim()
        val files = attachments.joinToString("\\n") { "📎 ${it.name}" }
        return listOf(base, files).filter { it.isNotBlank() }.joinToString("\\n")
            .ifBlank { "📎 附件" }
    }

    private fun buildModelText(text: String, attachments: List<PendingAttachment>): String = buildString {
        if (text.isNotBlank()) append(text.trim()) else append("請分析我附加的檔案。")
        attachments.forEach { a ->
            val safeName = a.name.replace('"', '\'')
            append("\\n\\n<<<JuyuAttachment name=\\\"").append(safeName)
                .append("\\" type=\\\"").append(a.mimeType).append("\\">>>\\n")
            when {
                a.textContent != null -> append(a.textContent)
                a.imageBase64 != null -> append("[圖片附件已隨本次訊息以影像輸入傳送給模型]")
                else -> append("[附件無可讀取內容]")
            }
            if (a.truncated) append("\\n[此附件內容已截斷]")
            append("\\n<<<EndJuyuAttachment>>>")
        }
    }

    private fun displayPersistedUserContent(content: String): String {
        val re = Regex("(?s)\\\\n?<<<JuyuAttachment name=\\\"([^\\\"]*)\\\" type=\\\"[^\\\"]*\\\">>>.*?<<<EndJuyuAttachment>>>")
        return re.replace(content) { m -> "\\n📎 ${m.groupValues[1]}" }.trim()
    }
'''
anchor='''    /** Map a persisted [MessageEntity] back to its UI item, carrying the row id for actions. */'''
s=must_replace(s,anchor,helper+'\n'+anchor,'attachment helpers')
write(p,s)

p='app/src/main/kotlin/com/phoneai/agent/ui/chat/ChatScreen.kt'; s=read(p)
s=s.replace('import androidx.compose.material.icons.filled.Add\n','import androidx.compose.material.icons.filled.Add\nimport androidx.compose.material.icons.filled.AttachFile\n')
s=s.replace('import androidx.lifecycle.viewmodel.compose.viewModel\n','import androidx.activity.compose.rememberLauncherForActivityResult\nimport androidx.activity.result.contract.ActivityResultContracts\nimport androidx.lifecycle.viewmodel.compose.viewModel\n')
s=s.replace('import androidx.compose.ui.platform.LocalClipboardManager\n','import androidx.compose.ui.platform.LocalClipboardManager\nimport androidx.compose.ui.platform.LocalContext\n')
s=s.replace('import com.phoneai.agent.accessibility.AccessibilityDeviceController\n','import com.phoneai.agent.accessibility.AccessibilityDeviceController\nimport com.phoneai.agent.attachment.AttachmentReader\nimport com.phoneai.agent.attachment.PendingAttachment\n')
old='''    val scope = rememberCoroutineScope()
    val clipboard = LocalClipboardManager.current
    // The user message currently open in the edit dialog, if any.
    var editing by remember { mutableStateOf<ChatMessageItem?>(null) }'''
new='''    val scope = rememberCoroutineScope()
    val clipboard = LocalClipboardManager.current
    val context = LocalContext.current
    var attachments by remember { mutableStateOf<List<PendingAttachment>>(emptyList()) }
    var attachmentLoading by remember { mutableStateOf(false) }
    var attachmentError by remember { mutableStateOf<String?>(null) }
    val attachmentLauncher = rememberLauncherForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        if (uris.isNotEmpty()) {
            scope.launch {
                attachmentLoading = true
                attachmentError = null
                val loaded = mutableListOf<PendingAttachment>()
                for (uri in uris.take(5)) {
                    try {
                        loaded += AttachmentReader.read(context, uri)
                    } catch (e: Exception) {
                        attachmentError = "${uri.lastPathSegment ?: "附件"}：${e.message ?: "讀取失敗"}"
                    }
                }
                attachments = (attachments + loaded).take(5)
                attachmentLoading = false
            }
        }
    }
    // The user message currently open in the edit dialog, if any.
    var editing by remember { mutableStateOf<ChatMessageItem?>(null) }'''
s=must_replace(s,old,new,'ChatScreen state/launcher')
old='''            InputRow(
                text = state.input,
                isRunning = state.isRunning,
                onText = vm::setInput,
                onSend = { vm.send() },
                onCancel = vm::cancel
            )'''
new='''            InputRow(
                text = state.input,
                isRunning = state.isRunning,
                attachments = attachments,
                attachmentLoading = attachmentLoading,
                attachmentError = attachmentError,
                onText = vm::setInput,
                onPickAttachments = { attachmentLauncher.launch(arrayOf("*/*")) },
                onRemoveAttachment = { target -> attachments = attachments.filterNot { it === target } },
                onSend = {
                    vm.sendWithAttachments(attachments)
                    attachments = emptyList()
                    attachmentError = null
                },
                onCancel = vm::cancel
            )'''
s=must_replace(s,old,new,'InputRow invocation')
old='''private fun InputRow(
    text: String,
    isRunning: Boolean,
    onText: (String) -> Unit,
    onSend: () -> Unit,
    onCancel: () -> Unit,
    phrases: List<com.phoneai.agent.data.QuickPhrase> = emptyList()
) {'''
new='''private fun InputRow(
    text: String,
    isRunning: Boolean,
    attachments: List<PendingAttachment>,
    attachmentLoading: Boolean,
    attachmentError: String?,
    onText: (String) -> Unit,
    onPickAttachments: () -> Unit,
    onRemoveAttachment: (PendingAttachment) -> Unit,
    onSend: () -> Unit,
    onCancel: () -> Unit,
    phrases: List<com.phoneai.agent.data.QuickPhrase> = emptyList()
) {'''
s=must_replace(s,old,new,'InputRow signature')
old='''    Surface(
        tonalElevation = 3.dp,
        modifier = Modifier.fillMaxWidth()
    ) {
        Row(
            modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
            verticalAlignment = Alignment.Bottom,
            horizontalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            // Quick phrases: insert a saved snippet into the input rather than sending it, so'''
new='''    Surface(
        tonalElevation = 3.dp,
        modifier = Modifier.fillMaxWidth()
    ) {
        Column(modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp)) {
            if (attachments.isNotEmpty()) {
                attachments.forEach { attachment ->
                    Surface(
                        modifier = Modifier.fillMaxWidth().padding(bottom = 4.dp),
                        shape = RoundedCornerShape(10.dp),
                        color = MaterialTheme.colorScheme.surfaceVariant
                    ) {
                        Row(
                            modifier = Modifier.fillMaxWidth().padding(start = 10.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Text(
                                "📎 ${attachment.name}",
                                modifier = Modifier.weight(1f),
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                                style = MaterialTheme.typography.bodySmall
                            )
                            IconButton(
                                onClick = { onRemoveAttachment(attachment) },
                                enabled = !isRunning,
                                modifier = Modifier.size(32.dp)
                            ) {
                                Icon(Icons.Default.Close, contentDescription = stringResource(R.string.chat_attachment_remove), modifier = Modifier.size(16.dp))
                            }
                        }
                    }
                }
            }
            attachmentError?.let {
                Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(bottom = 4.dp))
            }
            Row(
                verticalAlignment = Alignment.Bottom,
                horizontalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                IconButton(
                    onClick = onPickAttachments,
                    enabled = !isRunning && !attachmentLoading && attachments.size < 5,
                    modifier = Modifier.testTag("btn_attach")
                ) {
                    if (attachmentLoading) {
                        CircularProgressIndicator(modifier = Modifier.size(20.dp), strokeWidth = 2.dp)
                    } else {
                        Icon(Icons.Default.AttachFile, contentDescription = stringResource(R.string.chat_cd_attach_file))
                    }
                }
            // Quick phrases: insert a saved snippet into the input rather than sending it, so'''
s=must_replace(s,old,new,'InputRow layout start')
old='''                FilledIconButton(
                    onClick = { focusManager.clearFocus(); keyboard?.hide(); onSend() },
                    enabled = text.isNotBlank(),'''
new='''                FilledIconButton(
                    onClick = { focusManager.clearFocus(); keyboard?.hide(); onSend() },
                    enabled = (text.isNotBlank() || attachments.isNotEmpty()) && !attachmentLoading,'''
s=must_replace(s,old,new,'send enable attachments')
old='''            }
        }
    }
}

@Composable
private fun MessageRow'''
new='''            }
        }
        }
    }
}

@Composable
private fun MessageRow'''
s=must_replace(s,old,new,'InputRow closing braces')
write(p,s)

for pth in ['app/src/main/res/values/strings.xml','app/src/main/res/values-zh-rCN/strings.xml']:
    x=read(pth)
    if 'lang_chinese_traditional_name' not in x:
        x=x.replace('<string name="lang_chinese_name">', '<string name="lang_chinese_traditional_name">繁體中文</string>\n    <string name="lang_chinese_name">',1)
    if 'chat_cd_attach_file' not in x:
        block = '''\n    <string name="chat_cd_attach_file">Attach files</string>\n    <string name="chat_attachment_remove">Remove attachment</string>\n''' if 'values/strings' in pth else '''\n    <string name="chat_cd_attach_file">新增附件</string>\n    <string name="chat_attachment_remove">移除附件</string>\n'''
        x=x.replace('</resources>',block+'</resources>')
    write(pth,x)

p='app/src/main/kotlin/com/phoneai/agent/ui/settings/LanguageScreen.kt'; s=read(p)
if 'LanguageOption("zh-TW"' not in s:
    s=s.replace('LanguageOption("zh-CN", R.string.lang_chinese_name),','LanguageOption("zh-CN", R.string.lang_chinese_name),\n    LanguageOption("zh-TW", R.string.lang_chinese_traditional_name),')
write(p,s)

from opencc import OpenCC
cc=OpenCC('s2twp')
zh=cc.convert(read('app/src/main/res/values-zh-rCN/strings.xml'))

def set_string(xml,name,value):
    val=escape(value, {'"':'&quot;'})
    pat=re.compile(r'(<string name="'+re.escape(name)+r'">).*?(</string>)',re.S)
    if not pat.search(xml):
        xml=xml.replace('</resources>',f'    <string name="{name}">{val}</string>\n</resources>')
    else:
        xml=pat.sub(lambda m:m.group(1)+val+m.group(2),xml,1)
    return xml

overrides={
'app_name':'居魚','accessibility_service_label':'居魚控制服務','settings_title':'設定',
'group_general':'一般','group_models':'模型與服務','row_providers_title':'模型提供者',
'row_providers_subtitle':'設定 API 端點、金鑰與模型','row_instructions_title':'自訂指示',
'row_instructions_subtitle':'啟用後會附加到系統提示詞','common_add':'新增','common_save':'儲存',
'common_delete':'刪除','common_edit':'編輯','chat_cd_new_chat':'新對話',
'chat_no_provider':'尚未設定模型提供者，請點選右上角「設定」新增。','chat_status_ready':'就緒',
'chat_no_conversations':'尚無對話','mode_agent_title':'手機 Agent','mode_chat_title':'聊天問答',
'caps_ui_title':'介面控制','caps_device_title':'裝置直接控制',
'caps_device_desc':'讀取電量／網路／儲存空間、調整音量、設定鬧鐘。',
'provider_section_title':'模型提供者','provider_add_title':'新增模型','provider_edit_title':'編輯模型',
'provider_key_show':'顯示','provider_key_hide':'隱藏','assistant_cd_new':'新增助理',
'assistant_edit_title':'編輯助理','assistant_ctx_limit_title':'限制上下文長度',
'assistant_ctx_count_label':'上下文訊息數量','mcp_cd_add':'新增伺服器',
'mcp_add_title':'新增 MCP 伺服器','mcp_edit_title':'編輯 MCP 伺服器',
'mcp_status_connected':'已連線','mcp_status_connecting':'連線中','mcp_status_error':'連線失敗',
'mcp_status_idle':'未連線','phrase_add_title':'新增快速短語','phrase_edit_title':'編輯快速短語',
'instruction_add_title':'新增指示卡片','instruction_edit_title':'編輯指示卡片',
'instruction_content_label':'指示內容',
'instruction_hint':'已啟用的指示卡片會附加到系統提示詞後方，並套用於所有模式。可同時啟用多張指示卡片。',
'instruction_empty':'尚未建立指示卡片。\\n點選右上角「＋」新增一張。','instruction_no_title':'(未命名)',
'debug_title':'除錯 · 裝置控制','debug_output':'輸出','lang_chinese_name':'簡體中文',
'lang_chinese_traditional_name':'繁體中文','chat_cd_attach_file':'新增附件',
'chat_attachment_remove':'移除附件','about_row_github':'GitHub 儲存庫','about_row_check_update':'檢查更新'
}
for k,v in overrides.items(): zh=set_string(zh,k,v)
for a,b in [('設置','設定'),('添加','新增'),('屏幕','螢幕'),('網絡','網路'),('存儲','儲存'),('密鑰','金鑰'),('設備','裝置'),('服務器','伺服器'),('遠程','遠端'),('支持','支援'),('調試','除錯'),('默認','預設'),('信息','資訊'),('配置','設定')]:
    zh=zh.replace(a,b)
write('app/src/main/res/values-zh-rTW/strings.xml',zh)

p='app/src/main/kotlin/com/phoneai/agent/chat/ChatViewModel.kt'
write(p,read(p).replace('新会话','新對話'))
print('Juyu custom patch applied successfully')