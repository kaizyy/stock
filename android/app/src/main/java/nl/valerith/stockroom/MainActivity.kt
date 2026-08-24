package nl.valerith.stockroom

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.Inventory2
import androidx.compose.material.icons.filled.Login
import androidx.compose.material.icons.filled.Logout
import androidx.compose.material.icons.filled.North
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.South
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.nio.charset.StandardCharsets
import java.text.NumberFormat
import java.time.Instant
import java.util.Locale
import java.util.UUID

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { StockroomTheme { StockroomApp() } }
    }
}

private val StockGreen = Color(0xFF235C46)
private val StockBackground = Color(0xFFF5F7F4)

@Composable
private fun StockroomTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = lightColorScheme(
            primary = StockGreen,
            onPrimary = Color.White,
            background = StockBackground,
            surface = Color.White,
            onSurface = Color(0xFF18221E),
            secondary = Color(0xFF6F7C75),
        ),
        content = content,
    )
}

data class Permissions(
    val manageMembers: Boolean = false,
    val assignAdmin: Boolean = false,
    val manageItems: Boolean = false,
    val incoming: Boolean = false,
    val outgoing: Boolean = false,
    val readOnly: Boolean = false,
    val audit: Boolean = false,
    val createStockroom: Boolean = false,
)

data class Me(
    val name: String,
    val email: String,
    val stockroomName: String,
    val role: String,
    val permissions: Permissions,
)

data class StockItem(
    val id: String,
    val name: String,
    val sku: String,
    val stock: Int,
    val buy: Double,
    val sell: Double,
    val archived: Boolean,
)

data class StockTransaction(
    val id: String,
    val type: String,
    val itemId: String,
    val qty: Int,
    val price: Double,
    val salePrice: Double?,
    val party: String,
    val done: Boolean,
    val paid: Boolean,
    val date: String,
)

data class StockState(
    val raw: JSONObject,
    val items: List<StockItem>,
    val transactions: List<StockTransaction>,
)

private class ApiException(val code: Int, message: String) : Exception(message)

private class SessionStore(context: Context) {
    private val prefs = context.getSharedPreferences("stockroom_mobile_session", Context.MODE_PRIVATE)

    fun loadToken(): String? {
        val token = prefs.getString("token", null) ?: return null
        val expires = prefs.getLong("expires", 0L)
        if (expires <= System.currentTimeMillis() / 1000L) {
            clear()
            return null
        }
        return token
    }

    fun save(token: String, expiresAt: Long) {
        prefs.edit().putString("token", token).putLong("expires", expiresAt).apply()
    }

    fun clear() = prefs.edit().clear().apply()
}

private object StockroomApi {
    private val baseUrl = BuildConfig.STOCKROOM_BASE_URL.trimEnd('/')

    private suspend fun request(
        path: String,
        method: String = "GET",
        token: String? = null,
        form: Map<String, String>? = null,
        json: String? = null,
    ): String = withContext(Dispatchers.IO) {
        val connection = URL(baseUrl + path).openConnection() as HttpURLConnection
        connection.requestMethod = method
        connection.connectTimeout = 15_000
        connection.readTimeout = 20_000
        connection.setRequestProperty("Accept", "application/json")
        if (token != null) connection.setRequestProperty("Cookie", "stockroom_session=$token")

        val body = when {
            form != null -> form.entries.joinToString("&") {
                URLEncoder.encode(it.key, StandardCharsets.UTF_8.name()) + "=" +
                    URLEncoder.encode(it.value, StandardCharsets.UTF_8.name())
            }.also { connection.setRequestProperty("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8") }
            json != null -> json.also { connection.setRequestProperty("Content-Type", "application/json; charset=UTF-8") }
            else -> null
        }
        if (body != null) {
            connection.doOutput = true
            connection.outputStream.use { it.write(body.toByteArray(StandardCharsets.UTF_8)) }
        }

        val code = connection.responseCode
        val stream = if (code in 200..299) connection.inputStream else connection.errorStream
        val text = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
        connection.disconnect()
        if (code !in 200..299) {
            val message = runCatching { JSONObject(text).optString("error") }.getOrNull()
                ?.takeIf { it.isNotBlank() }
                ?: "Serverfout ($code)"
            throw ApiException(code, message)
        }
        text
    }

    suspend fun login(email: String, password: String): Pair<String, Long> {
        val obj = JSONObject(request("/api/mobile/login", "POST", form = mapOf("email" to email, "password" to password)))
        return obj.getString("token") to obj.getDouble("expiresAt").toLong()
    }

    suspend fun logout(token: String) {
        request("/api/mobile/logout", "POST", token = token, form = emptyMap())
    }

    suspend fun me(token: String): Me {
        val obj = JSONObject(request("/api/me", token = token))
        val user = obj.getJSONObject("user")
        val room = obj.getJSONObject("stockroom")
        val p = obj.getJSONObject("permissions")
        return Me(
            name = user.optString("name"),
            email = user.optString("email"),
            stockroomName = room.optString("name"),
            role = room.optString("role"),
            permissions = Permissions(
                manageMembers = p.optBoolean("manageMembers"),
                assignAdmin = p.optBoolean("assignAdmin"),
                manageItems = p.optBoolean("manageItems"),
                incoming = p.optBoolean("incoming"),
                outgoing = p.optBoolean("outgoing"),
                readOnly = p.optBoolean("readOnly"),
                audit = p.optBoolean("audit"),
                createStockroom = p.optBoolean("createStockroom"),
            ),
        )
    }

    suspend fun state(token: String): StockState = parseState(JSONObject(request("/api/state", token = token)))

    suspend fun saveState(token: String, raw: JSONObject) {
        request("/api/state", "PUT", token = token, json = raw.toString())
    }
}

private fun parseState(raw: JSONObject): StockState {
    val itemArray = raw.optJSONArray("items") ?: JSONArray()
    val txArray = raw.optJSONArray("transactions") ?: JSONArray()
    val items = buildList {
        for (i in 0 until itemArray.length()) {
            val o = itemArray.optJSONObject(i) ?: continue
            add(
                StockItem(
                    id = o.optString("id"),
                    name = o.optString("name", "Item"),
                    sku = o.optString("sku"),
                    stock = o.optInt("stock"),
                    buy = o.optDouble("buy", 0.0),
                    sell = o.optDouble("sell", 0.0),
                    archived = o.optBoolean("archived", false),
                )
            )
        }
    }
    val transactions = buildList {
        for (i in 0 until txArray.length()) {
            val o = txArray.optJSONObject(i) ?: continue
            add(
                StockTransaction(
                    id = o.optString("id"),
                    type = o.optString("type"),
                    itemId = o.optString("itemId"),
                    qty = o.optInt("qty"),
                    price = o.optDouble("price", 0.0),
                    salePrice = if (o.has("salePrice") && !o.isNull("salePrice")) o.optDouble("salePrice") else null,
                    party = o.optString("party"),
                    done = o.optBoolean("done", false),
                    paid = o.optBoolean("paid", false),
                    date = o.optString("date"),
                )
            )
        }
    }
    return StockState(raw, items, transactions)
}

@Composable
private fun StockroomApp() {
    val context = LocalContext.current
    val store = remember { SessionStore(context) }
    val scope = rememberCoroutineScope()
    var token by remember { mutableStateOf(store.loadToken()) }
    var me by remember { mutableStateOf<Me?>(null) }
    var state by remember { mutableStateOf<StockState?>(null) }
    var loading by remember { mutableStateOf(token != null) }
    var error by remember { mutableStateOf<String?>(null) }

    fun clearSession() {
        store.clear()
        token = null
        me = null
        state = null
        error = null
    }

    suspend fun reload(currentToken: String) {
        try {
            loading = true
            error = null
            me = StockroomApi.me(currentToken)
            state = StockroomApi.state(currentToken)
        } catch (e: ApiException) {
            if (e.code == 401) clearSession() else error = e.message
        } catch (e: Exception) {
            error = e.message ?: "Verbinding mislukt."
        } finally {
            loading = false
        }
    }

    LaunchedEffect(token) {
        token?.let { reload(it) }
    }

    if (token == null) {
        LoginScreen(
            error = error,
            onLogin = { email, password ->
                scope.launch {
                    loading = true
                    error = null
                    try {
                        val (newToken, expires) = StockroomApi.login(email.trim(), password)
                        store.save(newToken, expires)
                        token = newToken
                    } catch (e: Exception) {
                        error = e.message ?: "Inloggen mislukt."
                    } finally {
                        loading = false
                    }
                }
            },
            loading = loading,
        )
        return
    }

    if (loading && (me == null || state == null)) {
        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
        return
    }

    val currentMe = me
    val currentState = state
    if (currentMe == null || currentState == null) {
        ErrorScreen(error ?: "Gegevens konden niet worden geladen.") { token?.let { t -> scope.launch { reload(t) } } }
        return
    }

    MainScreen(
        me = currentMe,
        state = currentState,
        error = error,
        onRefresh = { scope.launch { token?.let { reload(it) } } },
        onLogout = {
            val current = token
            clearSession()
            if (current != null) scope.launch { runCatching { StockroomApi.logout(current) } }
        },
        onSaveState = { newRaw, onDone ->
            val current = token ?: return@MainScreen
            scope.launch {
                try {
                    error = null
                    StockroomApi.saveState(current, newRaw)
                    state = StockroomApi.state(current)
                    onDone(null)
                } catch (e: ApiException) {
                    if (e.code == 401) clearSession()
                    onDone(e.message ?: "Opslaan mislukt.")
                } catch (e: Exception) {
                    onDone(e.message ?: "Opslaan mislukt.")
                }
            }
        },
    )
}

@Composable
private fun LoginScreen(error: String?, loading: Boolean, onLogin: (String, String) -> Unit) {
    val context = LocalContext.current
    var email by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Box(Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.Center) {
            Card(Modifier.fillMaxWidth().widthIn(max = 460.dp)) {
                Column(Modifier.padding(24.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
                    Text("stockroom", style = MaterialTheme.typography.headlineMedium, color = StockGreen)
                    Text("Log in op jouw bedrijfsvoorraad.", color = MaterialTheme.colorScheme.secondary)
                    if (BuildConfig.STOCKROOM_BASE_URL.contains("example.nl")) {
                        Text("Stel STOCKROOM_BASE_URL in bij het bouwen van de app.", color = MaterialTheme.colorScheme.error)
                    }
                    error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
                    OutlinedTextField(
                        value = email,
                        onValueChange = { email = it },
                        modifier = Modifier.fillMaxWidth(),
                        label = { Text("E-mailadres") },
                        singleLine = true,
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Email),
                    )
                    OutlinedTextField(
                        value = password,
                        onValueChange = { password = it },
                        modifier = Modifier.fillMaxWidth(),
                        label = { Text("Wachtwoord") },
                        singleLine = true,
                        visualTransformation = PasswordVisualTransformation(),
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                    )
                    Button(
                        onClick = { onLogin(email, password) },
                        enabled = email.isNotBlank() && password.isNotBlank() && !loading,
                        modifier = Modifier.fillMaxWidth(),
                    ) {
                        if (loading) CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp)
                        else { Icon(Icons.Default.Login, null); Spacer(Modifier.width(8.dp)); Text("Inloggen") }
                    }
                    TextButton(
                        onClick = {
                            context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(BuildConfig.STOCKROOM_BASE_URL.trimEnd('/') + "/register")))
                        },
                        modifier = Modifier.align(Alignment.CenterHorizontally),
                    ) { Text("Nieuw account registreren") }
                }
            }
        }
    }
}

@Composable
private fun ErrorScreen(message: String, onRetry: () -> Unit) {
    Box(Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text(message, color = MaterialTheme.colorScheme.error)
            Button(onClick = onRetry) { Text("Opnieuw proberen") }
        }
    }
}

private enum class Screen { OVERVIEW, INVENTORY, INCOMING, OUTGOING }

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun MainScreen(
    me: Me,
    state: StockState,
    error: String?,
    onRefresh: () -> Unit,
    onLogout: () -> Unit,
    onSaveState: (JSONObject, (String?) -> Unit) -> Unit,
) {
    var screen by remember { mutableStateOf(Screen.OVERVIEW) }
    var showAdd by remember { mutableStateOf(false) }
    var saveError by remember { mutableStateOf<String?>(null) }

    val canSeeIncoming = me.permissions.incoming || me.role == "viewer"
    val canSeeOutgoing = me.permissions.outgoing || me.role == "viewer"
    val nav = buildList {
        add(Screen.OVERVIEW)
        add(Screen.INVENTORY)
        if (canSeeIncoming) add(Screen.INCOMING)
        if (canSeeOutgoing) add(Screen.OUTGOING)
    }
    if (screen !in nav) screen = Screen.OVERVIEW

    val canAdd = when (screen) {
        Screen.OVERVIEW -> me.permissions.manageItems || me.permissions.incoming || me.permissions.outgoing
        Screen.INVENTORY -> me.permissions.manageItems
        Screen.INCOMING -> me.permissions.incoming
        Screen.OUTGOING -> me.permissions.outgoing
    } && !me.permissions.readOnly

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text(me.stockroomName)
                        Text(roleLabel(me.role), style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.secondary)
                    }
                },
                actions = {
                    IconButton(onClick = onRefresh) { Icon(Icons.Default.Refresh, "Vernieuwen") }
                    IconButton(onClick = onLogout) { Icon(Icons.Default.Logout, "Uitloggen") }
                },
            )
        },
        bottomBar = {
            NavigationBar {
                nav.forEach { destination ->
                    NavigationBarItem(
                        selected = destination == screen,
                        onClick = { screen = destination },
                        icon = { Icon(screenIcon(destination), null) },
                        label = { Text(screenLabel(destination)) },
                    )
                }
            }
        },
        floatingActionButton = {
            if (canAdd) FloatingActionButton(onClick = { showAdd = true }) { Icon(Icons.Default.Add, "Toevoegen") }
        },
    ) { padding ->
        Column(Modifier.fillMaxSize().padding(padding)) {
            error?.let { AssistChip(onClick = onRefresh, label = { Text(it) }, modifier = Modifier.padding(horizontal = 16.dp)) }
            saveError?.let { Text(it, color = MaterialTheme.colorScheme.error, modifier = Modifier.padding(horizontal = 16.dp, vertical = 6.dp)) }
            when (screen) {
                Screen.OVERVIEW -> OverviewScreen(me, state)
                Screen.INVENTORY -> InventoryScreen(me, state)
                Screen.INCOMING -> TransactionsScreen(me, state, "incoming")
                Screen.OUTGOING -> TransactionsScreen(me, state, "outgoing")
            }
        }
    }

    if (showAdd) {
        AddDialog(
            me = me,
            state = state,
            initialScreen = screen,
            onDismiss = { showAdd = false },
            onSubmit = { action ->
                val result = applyAction(state.raw, action)
                if (result.second != null) {
                    saveError = result.second
                    return@AddDialog
                }
                onSaveState(result.first) { message ->
                    saveError = message
                    if (message == null) showAdd = false
                }
            },
        )
    }
}

@Composable
private fun OverviewScreen(me: Me, state: StockState) {
    val activeItems = state.items.filterNot { it.archived }
    val outgoing = state.transactions.filter { it.type == "outgoing" }
    val incoming = state.transactions.filter { it.type == "incoming" }
    val units = activeItems.sumOf { it.stock }
    val expected = incoming.count { !it.done }
    val unpaid = outgoing.filter { !it.done }
    val euro = remember { NumberFormat.getCurrencyInstance(Locale("nl", "NL")) }
    val buyValue = activeItems.sumOf { it.stock * it.buy }
    val sellValue = activeItems.sumOf { it.stock * it.sell }
    val sales = outgoing.sumOf { it.qty * it.price }
    val paidCosts = incoming.filter { it.paid }.sumOf { it.qty * it.price }

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item { Text("Overzicht", style = MaterialTheme.typography.headlineSmall) }
        item { MetricCard("Voorraad", "$units stuks", "${activeItems.size} actieve artikelen") }
        if (me.role in setOf("owner", "admin", "member")) {
            item { MetricCard("Inkoopwaarde", euro.format(buyValue), "Potentiële omzet ${euro.format(sellValue)}") }
            item { MetricCard("Daadwerkelijke omzet", euro.format(sales - paidCosts), "Verkopen minus betaalde inkoop") }
            item { MetricCard("Openstaand", euro.format(unpaid.sumOf { it.qty * it.price }), "${unpaid.size} betalingen te ontvangen") }
            item { MetricCard("Verwacht", expected.toString(), "Inkomende leveringen") }
        } else if (me.role == "buyer") {
            item { MetricCard("Verwacht", expected.toString(), "Inkomende leveringen") }
            item { MetricCard("Inkoopwaarde voorraad", euro.format(buyValue), "Operationele inkoopinformatie") }
        } else if (me.role == "seller") {
            item { MetricCard("Verkoop", euro.format(sales), "Totaal geregistreerde verkoop") }
            item { MetricCard("Openstaand", euro.format(unpaid.sumOf { it.qty * it.price }), "${unpaid.size} betalingen te ontvangen") }
        } else {
            item { MetricCard("Verwacht", expected.toString(), "Inkomende leveringen") }
            item { MetricCard("Openstaande verkopen", unpaid.size.toString(), "Alleen-lezen") }
        }
        item { Text("Recente activiteit", style = MaterialTheme.typography.titleMedium) }
        items(state.transactions.sortedByDescending { it.date }.take(8)) { tx ->
            val article = state.items.firstOrNull { it.id == tx.itemId }
            ListItem(
                headlineContent = { Text("${tx.qty}× ${article?.name ?: "Item"}") },
                supportingContent = { Text(if (tx.type == "incoming") "Inkomend${partySuffix(tx.party)}" else "Uitgaand${partySuffix(tx.party)}") },
                leadingContent = { Icon(if (tx.type == "incoming") Icons.Default.South else Icons.Default.North, null) },
            )
            HorizontalDivider()
        }
    }
}

@Composable
private fun MetricCard(title: String, value: String, subtitle: String) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(18.dp)) {
            Text(title, color = MaterialTheme.colorScheme.secondary)
            Text(value, style = MaterialTheme.typography.headlineMedium)
            Text(subtitle, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.secondary)
        }
    }
}

@Composable
private fun InventoryScreen(me: Me, state: StockState) {
    val euro = remember { NumberFormat.getCurrencyInstance(Locale("nl", "NL")) }
    val showBuy = me.role in setOf("owner", "admin", "member", "buyer")
    val showSell = me.role in setOf("owner", "admin", "member", "seller")
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        item { Text("Voorraad", style = MaterialTheme.typography.headlineSmall) }
        items(state.items.filterNot { it.archived }, key = { it.id }) { item ->
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(5.dp)) {
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                        Column { Text(item.name, style = MaterialTheme.typography.titleMedium); Text(item.sku, color = MaterialTheme.colorScheme.secondary) }
                        Text("${item.stock} stuks", style = MaterialTheme.typography.titleMedium)
                    }
                    if (showBuy) Text("Inkoop: ${euro.format(item.buy)}", style = MaterialTheme.typography.bodySmall)
                    if (showSell) Text("Verkoop: ${euro.format(item.sell)}", style = MaterialTheme.typography.bodySmall)
                }
            }
        }
        if (state.items.none { !it.archived }) item { Text("Nog geen artikelen.") }
    }
}

@Composable
private fun TransactionsScreen(me: Me, state: StockState, type: String) {
    val euro = remember { NumberFormat.getCurrencyInstance(Locale("nl", "NL")) }
    val rows = state.transactions.filter { it.type == type }.sortedByDescending { it.date }
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        item { Text(if (type == "incoming") "Inkomend" else "Uitgaand", style = MaterialTheme.typography.headlineSmall) }
        items(rows, key = { it.id }) { tx ->
            val item = state.items.firstOrNull { it.id == tx.itemId }
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(5.dp)) {
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                        Text(item?.name ?: "Onbekend item", style = MaterialTheme.typography.titleMedium)
                        Text("${tx.qty} stuks")
                    }
                    if (tx.party.isNotBlank()) Text(tx.party, color = MaterialTheme.colorScheme.secondary)
                    val maySeePrice = when (type) {
                        "incoming" -> me.role in setOf("owner", "admin", "member", "buyer")
                        else -> me.role in setOf("owner", "admin", "member", "seller")
                    }
                    if (maySeePrice) Text("${euro.format(tx.price)} per stuk · ${euro.format(tx.qty * tx.price)} totaal")
                    if (type == "incoming") {
                        Text(if (tx.done) "Geleverd" else "Niet geleverd")
                        Text(if (tx.paid) "Betaald" else "Niet betaald")
                    } else Text(if (tx.done) "Betaald" else "Niet betaald")
                }
            }
        }
        if (rows.isEmpty()) item { Text("Geen transacties.") }
    }
}

private sealed interface AddAction {
    data class Item(val name: String, val sku: String) : AddAction
    data class Incoming(
        val itemId: String,
        val qty: Int,
        val price: Double,
        val salePrice: Double,
        val party: String,
        val delivered: Boolean,
        val paid: Boolean,
    ) : AddAction
    data class Outgoing(
        val itemId: String,
        val qty: Int,
        val price: Double,
        val party: String,
        val paid: Boolean,
    ) : AddAction
}

@Composable
private fun AddDialog(me: Me, state: StockState, initialScreen: Screen, onDismiss: () -> Unit, onSubmit: (AddAction) -> Unit) {
    val allowed = buildList {
        if (me.permissions.manageItems) add("item")
        if (me.permissions.incoming) add("incoming")
        if (me.permissions.outgoing) add("outgoing")
    }
    val initial = when {
        initialScreen == Screen.INCOMING && "incoming" in allowed -> "incoming"
        initialScreen == Screen.OUTGOING && "outgoing" in allowed -> "outgoing"
        initialScreen == Screen.INVENTORY && "item" in allowed -> "item"
        else -> allowed.firstOrNull() ?: "item"
    }
    var type by remember { mutableStateOf(initial) }
    var name by remember { mutableStateOf("") }
    var sku by remember { mutableStateOf("") }
    val activeItems = state.items.filterNot { it.archived }
    var selectedId by remember { mutableStateOf(activeItems.firstOrNull()?.id.orEmpty()) }
    var itemMenu by remember { mutableStateOf(false) }
    var qty by remember { mutableStateOf("1") }
    var price by remember { mutableStateOf("") }
    var salePrice by remember { mutableStateOf("") }
    var party by remember { mutableStateOf("") }
    var deliveredOrPaid by remember { mutableStateOf(false) }
    var incomingPaid by remember { mutableStateOf(false) }
    var localError by remember { mutableStateOf<String?>(null) }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Toevoegen") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                if (allowed.size > 1) {
                    SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
                        allowed.forEachIndexed { index, option ->
                            SegmentedButton(
                                selected = type == option,
                                onClick = { type = option },
                                shape = SegmentedButtonDefaults.itemShape(index, allowed.size),
                            ) { Text(actionLabel(option)) }
                        }
                    }
                }
                localError?.let { Text(it, color = MaterialTheme.colorScheme.error) }
                if (type == "item") {
                    OutlinedTextField(name, { name = it }, label = { Text("Itemnaam") }, modifier = Modifier.fillMaxWidth())
                    OutlinedTextField(sku, { sku = it }, label = { Text("SKU") }, modifier = Modifier.fillMaxWidth())
                } else {
                    Box {
                        OutlinedButton(onClick = { itemMenu = true }, modifier = Modifier.fillMaxWidth()) {
                            Text(activeItems.firstOrNull { it.id == selectedId }?.name ?: "Kies artikel")
                        }
                        DropdownMenu(expanded = itemMenu, onDismissRequest = { itemMenu = false }) {
                            activeItems.forEach { item -> DropdownMenuItem(text = { Text("${item.name} (${item.stock})") }, onClick = { selectedId = item.id; itemMenu = false }) }
                        }
                    }
                    OutlinedTextField(qty, { qty = it }, label = { Text("Aantal") }, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number), modifier = Modifier.fillMaxWidth())
                    OutlinedTextField(price, { price = it }, label = { Text(if (type == "incoming") "Inkoopprijs per stuk" else "Verkoopprijs per stuk") }, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal), modifier = Modifier.fillMaxWidth())
                    if (type == "incoming") OutlinedTextField(salePrice, { salePrice = it }, label = { Text("Verkoopprijs per stuk") }, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal), modifier = Modifier.fillMaxWidth())
                    OutlinedTextField(party, { party = it }, label = { Text(if (type == "incoming") "Leverancier (optioneel)" else "Klant (optioneel)") }, modifier = Modifier.fillMaxWidth())
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Checkbox(deliveredOrPaid, { deliveredOrPaid = it })
                        Text(if (type == "incoming") "Al geleverd" else "Al betaald")
                    }
                    if (type == "incoming") Row(verticalAlignment = Alignment.CenterVertically) {
                        Checkbox(incomingPaid, { incomingPaid = it }); Text("Inkoop al betaald")
                    }
                }
            }
        },
        confirmButton = {
            Button(onClick = {
                localError = null
                when (type) {
                    "item" -> if (name.isBlank() || sku.isBlank()) localError = "Vul itemnaam en SKU in." else onSubmit(AddAction.Item(name.trim(), sku.trim()))
                    "incoming" -> {
                        val q = qty.toIntOrNull(); val p = price.replace(',', '.').toDoubleOrNull(); val s = salePrice.replace(',', '.').toDoubleOrNull()
                        if (selectedId.isBlank() || q == null || q <= 0 || p == null || p < 0 || s == null || s < 0) localError = "Controleer artikel, aantal en prijzen."
                        else onSubmit(AddAction.Incoming(selectedId, q, p, s, party.trim(), deliveredOrPaid, incomingPaid))
                    }
                    "outgoing" -> {
                        val q = qty.toIntOrNull(); val p = price.replace(',', '.').toDoubleOrNull()
                        if (selectedId.isBlank() || q == null || q <= 0 || p == null || p < 0) localError = "Controleer artikel, aantal en prijs."
                        else onSubmit(AddAction.Outgoing(selectedId, q, p, party.trim(), deliveredOrPaid))
                    }
                }
            }) { Text("Opslaan") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Annuleren") } },
    )
}

private fun applyAction(original: JSONObject, action: AddAction): Pair<JSONObject, String?> {
    val raw = JSONObject(original.toString())
    val items = raw.optJSONArray("items") ?: JSONArray().also { raw.put("items", it) }
    val tx = raw.optJSONArray("transactions") ?: JSONArray().also { raw.put("transactions", it) }

    when (action) {
        is AddAction.Item -> {
            items.put(JSONObject().apply {
                put("id", UUID.randomUUID().toString())
                put("name", action.name)
                put("sku", action.sku)
                put("stock", 0)
                put("buy", 0.0)
                put("sell", 0.0)
                put("archived", false)
            })
        }
        is AddAction.Incoming -> {
            val item = findItem(items, action.itemId) ?: return original to "Artikel niet gevonden."
            tx.put(JSONObject().apply {
                put("id", UUID.randomUUID().toString())
                put("type", "incoming")
                put("itemId", action.itemId)
                put("qty", action.qty)
                put("price", action.price)
                put("salePrice", action.salePrice)
                put("party", action.party)
                put("done", action.delivered)
                put("paid", action.paid)
                put("date", Instant.now().toString())
            })
            item.put("sell", action.salePrice)
            if (action.delivered) {
                item.put("stock", item.optInt("stock") + action.qty)
                item.put("buy", action.price)
            }
        }
        is AddAction.Outgoing -> {
            val item = findItem(items, action.itemId) ?: return original to "Artikel niet gevonden."
            val stock = item.optInt("stock")
            if (action.qty > stock) return original to "Niet genoeg voorraad voor deze verkoop."
            tx.put(JSONObject().apply {
                put("id", UUID.randomUUID().toString())
                put("type", "outgoing")
                put("itemId", action.itemId)
                put("qty", action.qty)
                put("price", action.price)
                put("party", action.party)
                put("done", action.paid)
                put("date", Instant.now().toString())
            })
            item.put("stock", stock - action.qty)
        }
    }
    return raw to null
}

private fun findItem(items: JSONArray, id: String): JSONObject? {
    for (i in 0 until items.length()) {
        val item = items.optJSONObject(i) ?: continue
        if (item.optString("id") == id) return item
    }
    return null
}

private fun partySuffix(value: String) = if (value.isBlank()) "" else " · $value"
private fun roleLabel(role: String) = when (role) {
    "owner" -> "Owner"
    "admin" -> "Admin"
    "member" -> "Gebruiker"
    "buyer" -> "Inkoper"
    "seller" -> "Verkoper"
    "viewer" -> "Viewer"
    else -> role
}
private fun screenLabel(screen: Screen) = when (screen) {
    Screen.OVERVIEW -> "Overzicht"
    Screen.INVENTORY -> "Voorraad"
    Screen.INCOMING -> "Inkomend"
    Screen.OUTGOING -> "Uitgaand"
}
private fun screenIcon(screen: Screen) = when (screen) {
    Screen.OVERVIEW -> Icons.Default.Home
    Screen.INVENTORY -> Icons.Default.Inventory2
    Screen.INCOMING -> Icons.Default.South
    Screen.OUTGOING -> Icons.Default.North
}
private fun actionLabel(type: String) = when (type) {
    "item" -> "Item"
    "incoming" -> "Inkoop"
    else -> "Verkoop"
}
