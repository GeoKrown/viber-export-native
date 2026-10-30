// Windows x64 / Qt 6.11 ABI. All statements are fixed read-only SELECTs.
// No key capture, database writes, UI actions, or outgoing Viber messages.
'use strict';
const cfg = __CONFIG__;
if (Process.platform !== 'windows' || Process.arch !== 'x64') throw Error('unsupported_platform');
const sql = Process.getModuleByName('Qt6Sql.dll');
const core = Process.getModuleByName('Qt6Core.dll');
function fn(m, name, ret, args) { return new NativeFunction(m.getExportByName(name), ret, args); }
const names = fn(sql, '?connectionNames@QSqlDatabase@@SA?AV?$QList@VQString@@@@XZ', 'void', ['pointer']);
const database = fn(sql, '?database@QSqlDatabase@@SA?AV1@AEBVQString@@_N@Z', 'void', ['pointer', 'pointer', 'bool']);
const dbName = fn(sql, '?databaseName@QSqlDatabase@@QEBA?AVQString@@XZ', 'void', ['pointer', 'pointer']);
const dbThread = fn(sql, '?thread@QSqlDatabase@@QEBAPEAVQThread@@XZ', 'pointer', ['pointer']);
const currentThread = fn(core, '?currentThread@QThread@@SAPEAV1@XZ', 'pointer', []);
const dbDriver = fn(sql, '?driver@QSqlDatabase@@QEBAPEAVQSqlDriver@@XZ', 'pointer', ['pointer']);
const queryDriver = fn(sql, '?driver@QSqlQuery@@QEBAPEBVQSqlDriver@@XZ', 'pointer', ['pointer']);
const make = fn(core, '?fromUtf16@QString@@SA?AV1@PEB_S_J@Z', 'void', ['pointer', 'pointer', 'int64']);
const freeString = fn(core, '??1QString@@QEAA@XZ', 'void', ['pointer']);
const freeArray = fn(core, '?deallocate@QArrayData@@SAXPEAU1@_J1@Z', 'void', ['pointer', 'int64', 'int64']);
const freeDb = fn(sql, '??1QSqlDatabase@@QEAA@XZ', 'void', ['pointer']);
const newQuery = fn(sql, '??0QSqlQuery@@QEAA@AEBVQSqlDatabase@@@Z', 'pointer', ['pointer', 'pointer']);
const freeQuery = fn(sql, '??1QSqlQuery@@QEAA@XZ', 'void', ['pointer']);
const exec = fn(sql, '?exec@QSqlQuery@@QEAA_NAEBVQString@@@Z', 'bool', ['pointer', 'pointer']);
const next = fn(sql, '?next@QSqlQuery@@QEAA_NXZ', 'bool', ['pointer']);
const value = fn(sql, '?value@QSqlQuery@@QEBA?AVQVariant@@H@Z', 'void', ['pointer', 'pointer', 'int']);
const stringify = fn(core, '?toString@QVariant@@QEBA?AVQString@@XZ', 'void', ['pointer', 'pointer']);
const freeVariant = fn(core, '??1QVariant@@QEAA@XZ', 'void', ['pointer']);
function text(p) {
    const n = p.add(16).readS64().toNumber();
    if (n < 0 || n > 65536) throw Error('invalid_qstring');
    return n ? p.add(8).readPointer().readUtf16String(n) : '';
}
function string(s) { const p = Memory.alloc(24); make(p, Memory.allocUtf16String(s), s.length); return p; }
function query(db, statement, columns, limit) {
    const q = Memory.alloc(32); newQuery(q, db);
    const s = string(statement); const result = [];
    try {
        if (!exec(q, s)) throw Error('select_failed');
        while (result.length < limit && next(q)) {
            const row = {};
            for (let i = 0; i < columns.length; i++) {
                const v = Memory.alloc(64); const t = Memory.alloc(24);
                value(q, v, i);
                try { stringify(v, t); try { row[columns[i]] = text(t); } finally { freeString(t); } }
                finally { freeVariant(v); }
            }
            result.push(row);
        }
        return result;
    } finally { freeString(s); freeQuery(q); }
}
let busy = false, finished = false;
const transactions = new Map();
function collect(eventReturned = false) {
    if (busy || finished) return;
    busy = true;
    const list = Memory.alloc(24); let count = 0, allocated = false;
    try {
        names(list); allocated = true;
        count = list.add(16).readS64().toNumber();
        if (count < 0 || count > 128) throw Error('connection_limit');
        let found = null;
        for (let i = 0; i < count; i++) {
            const db = Memory.alloc(16);
            database(db, list.add(8).readPointer().add(i * 24), 0);
            try {
                // Qt forbids using a database connection from another thread.
                if (!dbThread(db).equals(currentThread())) continue;
                const path = Memory.alloc(24); dbName(db, path);
                let location;
                try { location = text(path); } finally { freeString(path); }
                if (!/[\\/]viber\.db$/i.test(location)) continue;
                const driver = dbDriver(db).toString();
                // Incremental extraction requires an observed commit.
                // History snapshots can also be read after the owning thread's
                // event handler returns (not in the middle of an SQL operation).
                // Never enter a transaction observed to be open by this probe.
                if (!['list','media'].includes(cfg.mode) && transactions.get(driver) !== false &&
                    !(cfg.mode === 'history' && eventReturned && transactions.get(driver) !== true)) continue;
                if (found) throw Error('ambiguous_profile');
                if (cfg.mode === 'list') {
                    found = {profile_path: location, chats: query(db,
                        'SELECT ChatID, Name, Flags, PGType FROM ChatInfo WHERE Name IS NOT NULL AND Name <> \'\' ORDER BY ChatID LIMIT 2000',
                        ['chat_id', 'name', 'flags', 'pg_type'], 2000)};
                    if (cfg.media_schema === true) found.media_schema = query(db,
                        "SELECT sql FROM sqlite_master WHERE name='DownloadFile'", ['schema'], 1);
                } else if (cfg.mode === 'media') {
                    // Attachment status only; no body text or price import.
                    const ids = cfg.chat_ids.join(',');
                    const events = cfg.event_ids.join(',');
                    const groups = query(db, `SELECT ChatID, Name FROM ChatInfo WHERE ChatID IN (${ids})`, ['chat_id','name'], cfg.chat_ids.length);
                    const rows = query(db,
                        `SELECT e.EventID,e.Token,e.ChatID,e.TimeStamp,m.Type,m.PayloadPath,m.ThumbnailPath,` +
                        `(SELECT group_concat(d.TempFileName,char(10)) FROM DownloadFile d WHERE d.EventID=e.EventID) ` +
                        `FROM Events e JOIN Messages m ON m.EventID=e.EventID ` +
                        `WHERE e.ChatID IN (${ids}) AND e.EventID IN (${events}) ORDER BY e.EventID`,
                        ['event_id','token','chat_id','timestamp','message_type','payload_path','thumbnail_path','download_paths'], cfg.event_ids.length);
                    found = {profile_path:location,chats:groups,rows:rows};
                } else if (cfg.mode === 'history') {
                    const ids = cfg.chat_ids.join(',');
                    const groups = query(db, `SELECT ChatID, Name FROM ChatInfo WHERE ChatID IN (${ids})`, ['chat_id','name'], cfg.chat_ids.length);
                    if (groups.length !== cfg.chat_ids.length) throw Error('chat_not_found');
                    const rows = query(db,
                        `SELECT e.EventID,e.Token,e.ChatID,e.TimeStamp,e.Direction,m.Type,m.Body,m.Status,` +
                        `m.PayloadPath,m.ThumbnailPath,m.Subject,` +
                        `(SELECT group_concat(d.TempFileName,char(10)) FROM DownloadFile d WHERE d.EventID=e.EventID) ` +
                        `FROM Events e JOIN Messages m ON m.EventID=e.EventID ` +
                        `WHERE e.ChatID IN (${ids}) AND e.TimeStamp >= ${cfg.cutoff_ms} AND e.EventID < ${cfg.before_id} ` +
                        `ORDER BY e.EventID DESC LIMIT ${cfg.batch_size}`,
                        ['event_id','token','chat_id','timestamp','direction','message_type','body','status',
                         'payload_path','thumbnail_path','subject','download_paths'], cfg.batch_size);
                    found = {profile_path:location,chats:groups,rows:rows};
                } else {
                    const ids = cfg.chat_ids.join(',');
                    const upper = query(db, 'SELECT coalesce(max(EventID),0) FROM Events', ['id'], 1)[0].id;
                    const groups = query(db, `SELECT ChatID, Name FROM ChatInfo WHERE ChatID IN (${ids})`, ['chat_id','name'], cfg.chat_ids.length);
                    if (groups.length !== cfg.chat_ids.length) throw Error('chat_not_found');
                    const rows = query(db,
                        `SELECT e.EventID,e.Token,e.ChatID,e.TimeStamp,e.Direction,m.Type,m.Body,m.Status ` +
                        `FROM Events e JOIN Messages m ON m.EventID=e.EventID ` +
                        `WHERE e.ChatID IN (${ids}) AND e.EventID > ${cfg.after_id} AND e.EventID <= ${upper} ` +
                        `ORDER BY e.EventID LIMIT ${cfg.batch_size}`,
                        ['event_id','token','chat_id','timestamp','direction','message_type','body','status'], cfg.batch_size);
                    // Re-read a bounded tail in each selected chat to capture edits.
                    const recent = [];
                    if (cfg.recent_limit > 0) for (const id of cfg.chat_ids) recent.push(...query(db,
                        `SELECT e.EventID,e.Token,e.ChatID,e.TimeStamp,e.Direction,m.Type,m.Body,m.Status ` +
                        `FROM Events e JOIN Messages m ON m.EventID=e.EventID WHERE e.ChatID=${id} ` +
                        `AND e.EventID <= ${upper} ORDER BY e.EventID DESC LIMIT ${cfg.recent_limit}`,
                        ['event_id','token','chat_id','timestamp','direction','message_type','body','status'], cfg.recent_limit));
                    found = {profile_path: location, chats: groups, rows: rows, recent: recent, upper_id: upper};
                }
            } finally { freeDb(db); }
        }
        if (found) { finished = true; send({status: 'ok', ...found}); }
    } catch (_) { finished = true; send({status: 'error', error: 'native_query_failed'}); }
    finally {
        // connectionNames() returns a fresh QList<QString> (Qt 6, x64).
        // Destroy its strings and allocation rather than leaking on each probe.
        if (allocated && count >= 0 && count <= 128) {
            const data = list.add(8).readPointer();
            for (let i = 0; i < count; i++) freeString(data.add(i * 24));
            const allocation = list.readPointer();
            if (!allocation.isNull()) freeArray(allocation, 24, 8);
        }
        busy = false;
    }
}
function attach(symbol, callback) { Interceptor.attach(sql.getExportByName(symbol), callback); }
// Do not inspect or log SQL arguments: they may include private data or keys.
attach('?exec@QSqlQuery@@QEAA_NAEBVQString@@@Z', {
    onEnter(args) {
        if (busy || finished) return;
        try {
            this.driver = queryDriver(args[0]).toString();
            // Examine transaction-control words only. No SQL or bound values leave this process.
            const command = text(args[1]).trim();
            if (/^BEGIN\b/i.test(command)) this.transaction = true;
            else if (/^(COMMIT|END)\b/i.test(command) || /^ROLLBACK\s*(TRANSACTION\s*)?;?$/i.test(command)) this.transaction = false;
        } catch (_) {}
    },
    onLeave(ret) {
        if (this.transaction !== undefined && (ret.toInt32() & 255)) transactions.set(this.driver, this.transaction);
        collect();
    }
});
attach('?exec@QSqlQuery@@QEAA_NXZ', {onLeave() { collect(); }});
for (const [operation, symbol] of [
    ['begin','?transaction@QSqlDatabase@@QEAA_NXZ'],
    ['end','?commit@QSqlDatabase@@QEAA_NXZ'],
    ['end','?rollback@QSqlDatabase@@QEAA_NXZ']]) {
    attach(symbol, {
        onEnter(args) { this.db = args[0]; },
        onLeave(ret) {
            if (busy || finished || ret.toInt32() === 0) return;
            try { transactions.set(dbDriver(this.db).toString(), operation === 'begin'); }
            catch (_) { return; }
            collect();
        }
    });
}
if (['media','list','history'].includes(cfg.mode)) {
    // Observe event dispatch too, so an idle SQL connection can answer a bounded
    // metadata request. collect() still enforces database thread ownership.
    Interceptor.attach(core.getExportByName('?notifyInternal2@QCoreApplication@@CA_NPEAVQObject@@PEAVQEvent@@@Z'),
                       {onLeave() { collect(true); }});
}
send({status: 'ready'});
