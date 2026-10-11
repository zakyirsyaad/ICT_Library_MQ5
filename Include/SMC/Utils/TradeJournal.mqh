//+------------------------------------------------------------------+
//|                                               TradeJournal.mqh   |
//|                     Append-only JSON Lines writer for trades     |
//+------------------------------------------------------------------+
#ifndef __SMC_TRADE_JOURNAL_MQH__
#define __SMC_TRADE_JOURNAL_MQH__

#include <SMC/Utils/SnapshotExporter.mqh>

#define SMC_TRADE_JOURNAL_VERSION "1.0"

// MQL5 has no nullable bool, so inOte uses a tri-state. UNSET serializes as
// JSON null and is NOT the same as NO: null means the producing EA never
// evaluated the OTE zone, while NO means it evaluated it and price was outside.
enum ENUM_SMC_JOURNAL_FLAG
  {
   SMC_JOURNAL_UNSET = -1,
   SMC_JOURNAL_NO = 0,
   SMC_JOURNAL_YES = 1
  };

// One closed trade. Field order matches the `required` list of
// schemas/trade-journal.schema.json. Nullable doubles use EMPTY_VALUE.
//
// String fields marked "schema enum" must carry one of the listed tokens. They
// are plain strings here because the schema owns the vocabulary, but writing
// anything else makes the Python analyzer reject the whole row, so the value
// must come from SessionToken()/TrendToken() below rather than from
// CSmcKillZone::GetSessionName() or SmcDirectionName(), which return
// human-readable names the schema does not accept.
struct SmcTradeRecord
  {
   long     ticket;
   long     positionId;
   string   symbol;
   string   timeframe;
   long     magic;              // DEAL_MAGIC is a long; do not narrow it
   string   variant;
   string   configHash;
   datetime openTime;
   datetime closeTime;
   int      direction;          // +1 long, -1 short
   int      barsHeld;
   double   lot;
   double   entryPrice;
   double   sl;                 // 0 = not set
   double   tp;                 // 0 = not set
   double   exitPrice;
   string   exitReason;         // schema enum: sl tp signal time_stop be_stop manual unknown
   double   spreadAtEntry;
   double   slippagePoints;
   double   grossProfit;
   double   commission;
   double   swap;
   double   netProfit;
   double   balanceAtOpen;
   double   equityAtOpen;
   double   marginUsed;
   double   intendedRiskPct;
   double   realizedRiskPct;
   double   mfe;                // EMPTY_VALUE = null
   double   mae;                // EMPTY_VALUE = null
   string   htfTrend;           // schema enum: bullish bearish ranging unknown
   double   zonePercent;        // EMPTY_VALUE = null
   ENUM_SMC_JOURNAL_FLAG inOte; // SMC_JOURNAL_UNSET = null
   string   zoneTypes;          // pipe-delimited; "none" or "unknown", never ""
   string   trigger;            // schema enum: displacement mss po3_distribution liq_sweep none unknown
   string   session;            // schema enum: Asian London NewYork ldn_ny_overlap none unknown
   double   confluenceScore;
   int      confluenceCount;

   // Initialises every field, following SmcRecord::Init(). Two reasons to be
   // explicit rather than relying on zero-initialisation:
   //
   // 1. The four nullable fields must default to "not measured". Left at zero
   //    they would assert mfe 0.0 ("price never moved favourably"),
   //    zonePercent 0.0 ("price sat at the swing low") and inOte false
   //    ("evaluated, outside the zone") -- three fabricated measurements that
   //    Validate() accepts and the analyzer reports as findings. Spec §9
   //    requires null when CopyRates fails.
   // 2. Whether MQL5 zero-fills members a constructor does not assign is not
   //    something this file should depend on. An indeterminate but finite
   //    garbage value passes Validate and is emitted as a huge number, because
   //    NullableNumber only special-cases EMPTY_VALUE.
   SmcTradeRecord()
     {
      ticket = 0;
      positionId = 0;
      symbol = "";
      timeframe = "";
      magic = 0;
      variant = "";
      configHash = "";
      openTime = 0;
      closeTime = 0;
      direction = 0;
      barsHeld = 0;
      lot = 0.0;
      entryPrice = 0.0;
      sl = 0.0;
      tp = 0.0;
      exitPrice = 0.0;
      exitReason = "";
      spreadAtEntry = 0.0;
      slippagePoints = 0.0;
      grossProfit = 0.0;
      commission = 0.0;
      swap = 0.0;
      netProfit = 0.0;
      balanceAtOpen = 0.0;
      equityAtOpen = 0.0;
      marginUsed = 0.0;
      intendedRiskPct = 0.0;
      realizedRiskPct = 0.0;
      mfe = EMPTY_VALUE;
      mae = EMPTY_VALUE;
      htfTrend = "";
      zonePercent = EMPTY_VALUE;
      inOte = SMC_JOURNAL_UNSET;
      zoneTypes = "";
      trigger = "";
      session = "";
      confluenceScore = 0.0;
      confluenceCount = 0;
     }
  };

// Writes one closed trade per line to a JSON Lines journal under FILE_COMMON.
//
// Encoding: FILE_BIN with an explicit CP_UTF8 char array, matching
// CSmcSnapshotExporter. Per the MQL5 reference, a text-mode file opened WITHOUT
// FILE_ANSI is handled as Unicode with no conversion, so FILE_TXT alone does not
// give UTF-8 either. FILE_ANSI with CP_UTF8 also works -- tests/TestHarness.mqh
// uses it -- but FILE_BIN plus an explicit array keeps the byte count checkable
// and matches the exporter this file is modelled on. FILE_UNICODE must never be
// used here: it produces UTF-16 with a BOM, which the Python analyzer rejects
// outright as JournalFormatError.
class CSmcTradeJournal
  {
public:
   static string Version()
     {
      return SMC_TRADE_JOURNAL_VERSION;
     }

   // Canonical session token for the journal's `session` field.
   //
   // CSmcKillZone::GetSessionName returns "New York", "LDN-NY Overlap" and
   // "None", three of whose five values the schema rejects; SmcConfig itself
   // uses "NewYork" for the same session. This mapping is the single place that
   // resolves the two vocabularies, and TestTradeJournal asserts every enum
   // value lands inside the schema.
   static string SessionToken(const ENUM_SMC_SESSION session)
     {
      switch(session)
        {
         case SESSION_ASIAN:     return "Asian";
         case SESSION_LONDON:    return "London";
         case SESSION_NEWYORK:   return "NewYork";
         case SESSION_LDN_NY_OL: return "ldn_ny_overlap";
         case SESSION_NONE:      return "none";
        }
      return "unknown";
     }

   // Canonical trend token. SmcDirectionName returns "neutral" for a ranging
   // market; the schema's token is "ranging".
   static string TrendToken(const ENUM_SMC_TREND trend)
     {
      switch(trend)
        {
         case SMC_TREND_BULLISH: return "bullish";
         case SMC_TREND_BEARISH: return "bearish";
         case SMC_TREND_RANGING: return "ranging";
        }
      return "unknown";
     }

   // Name for one run's journal. Call this once per run and pass the result to
   // Open(); calling it per append would scatter one run across many files.
   //
   // magic, chart and start time together keep concurrent writers apart. That
   // reduces but does not eliminate collisions between parallel Strategy
   // Tester optimization agents, which spec §11 places out of scope.
   static string JournalFilename(const long magic)
     {
      MqlDateTime now;
      TimeToStruct(TimeLocal(), now);
      // IntegerToString rather than %I64d: MQL5's StringFormat follows the C
      // conventions where the plain specifier is 32-bit, and a silently
      // truncated magic number would put two runs in the same file.
      return "trade-journal-" + IntegerToString(magic) + "-" +
             IntegerToString(ChartID()) + "-" +
             StringFormat("%04d%02d%02d-%02d%02d%02d", now.year, now.mon, now.day,
                          now.hour, now.min, now.sec) + ".jsonl";
     }

   // Opens filename for appending. Returns false and leaves handle invalid
   // when the name is unsafe or the file cannot be opened.
   static bool Open(const string filename, int &handle)
     {
      handle = INVALID_HANDLE;
      if(!CSmcSnapshotExporter::SafePath(filename))
         return false;
      // FILE_READ alongside FILE_WRITE is what preserves existing content.
      // FILE_WRITE on its own truncates the file at open, which would erase
      // the whole journal on every EA restart.
      const int opened = FileOpen(filename, FILE_READ | FILE_WRITE | FILE_BIN | FILE_COMMON);
      if(opened == INVALID_HANDLE)
         return false;
      // FileSeek returns bool, not a position. Comparing it against 0 would be
      // a dead check that never fires on failure.
      if(!FileSeek(opened, 0, SEEK_END))
        {
         FileClose(opened);
         return false;
        }
      handle = opened;
      return true;
     }

   // Appends one pre-serialised line. The caller supplies the trailing
   // newline so a partial write can only ever damage the final line.
   static bool AppendLine(const int handle, const string &line)
     {
      if(handle == INVALID_HANDLE)
         return false;
      uchar bytes[];
      int count = StringToCharArray(line, bytes, 0, WHOLE_ARRAY, CP_UTF8);
      count--; // StringToCharArray appends a terminating NUL that must not be written.
      if(count <= 0)
         return false;
      ResetLastError();
      // FileWriteArray, not FileWriteString. In FILE_BIN mode FileWriteString
      // writes the string's internal UTF-16 representation, which puts null
      // bytes in the journal and makes the Python analyzer reject the whole
      // file as UTF-16.
      const uint written = FileWriteArray(handle, bytes, 0, count);
      // Flush before sampling the error, matching CSmcSnapshotExporter::Export.
      // A full volume or a device fault surfaces at flush time, and checking
      // first would report success for a line that never reached the disk.
      // Flushing per line is deliberate: spec §10's guarantee that a crash
      // damages only the trailing line depends on nothing being buffered.
      FileFlush(handle);
      return written == (uint)count && GetLastError() == 0;
     }

   // Serialises and appends one closed trade. Returns false rather than
   // writing a record the analyzer would have to reject.
   static bool AppendTrade(const int handle, const SmcTradeRecord &rec)
     {
      string json;
      if(!SerializeTrade(rec, json))
         return false;
      // MQL5 rejects a temporary expression for a reference parameter, even a
      // const one, so the newline is joined into a named variable first.
      string payload = json + "\n";
      return AppendLine(handle, payload);
     }

   // FNV-1a 64-bit over a canonical parameter string, as 16 lowercase hex
   // characters. Not cryptographic and not meant to be: it only has to tell
   // two parameter sets apart so a saved report can be traced to its inputs.
   static string ConfigHash(const string &inputs)
     {
      ulong hash = 0xcbf29ce484222325;
      const int length = StringLen(inputs);
      for(int i = 0; i < length; i++)
        {
         hash ^= (ulong)StringGetCharacter(inputs, i);
         hash *= 0x100000001b3;
        }
      // Format the two 32-bit halves separately. StringFormat("%016x", hash)
      // silently prints only the low word, because %x is 32-bit under the C
      // conventions MQL5 follows -- the result is still 16 characters wide, so
      // a length check cannot catch the truncation.
      return StringFormat("%08x%08x", (uint)(hash >> 32), (uint)(hash & 0xFFFFFFFF));
     }

   // Builds one JSON object without a trailing newline. Public so a test can
   // assert the exact bytes, and so the output can be fed to the Python
   // analyzer to prove the two sides agree on the contract.
   static bool SerializeTrade(const SmcTradeRecord &rec, string &line)
     {
      // Clear first, matching CSmcSnapshotExporter::Serialize. A caller that
      // reuses one buffer across trades would otherwise keep the previous
      // trade's JSON after a rejection, and writing that buffer would
      // duplicate a trade.
      line = "";
      if(!Validate(rec))
         return false;
      line = "{\"journal_version\":" + Quote(Version()) +
         ",\"ticket\":" + IntegerToString(rec.ticket) +
         ",\"position_id\":" + IntegerToString(rec.positionId) +
         ",\"symbol\":" + Quote(rec.symbol) +
         ",\"timeframe\":" + Quote(rec.timeframe) +
         ",\"magic\":" + IntegerToString(rec.magic) +
         ",\"variant\":" + Quote(rec.variant) +
         ",\"config_hash\":" + Quote(rec.configHash) +
         ",\"open_time\":" + Timestamp(rec.openTime) +
         ",\"close_time\":" + Timestamp(rec.closeTime) +
         ",\"direction\":" + IntegerToString(rec.direction) +
         ",\"bars_held\":" + IntegerToString(rec.barsHeld) +
         ",\"lot\":" + Number(rec.lot) +
         ",\"entry_price\":" + Number(rec.entryPrice) +
         ",\"sl\":" + Number(rec.sl) +
         ",\"tp\":" + Number(rec.tp) +
         ",\"exit_price\":" + Number(rec.exitPrice) +
         ",\"exit_reason\":" + Quote(rec.exitReason) +
         ",\"spread_at_entry\":" + Number(rec.spreadAtEntry) +
         ",\"slippage_points\":" + Number(rec.slippagePoints) +
         ",\"gross_profit\":" + Number(rec.grossProfit) +
         ",\"commission\":" + Number(rec.commission) +
         ",\"swap\":" + Number(rec.swap) +
         ",\"net_profit\":" + Number(rec.netProfit) +
         ",\"balance_at_open\":" + Number(rec.balanceAtOpen) +
         ",\"equity_at_open\":" + Number(rec.equityAtOpen) +
         ",\"margin_used\":" + Number(rec.marginUsed) +
         ",\"intended_risk_pct\":" + Number(rec.intendedRiskPct) +
         ",\"realized_risk_pct\":" + Number(rec.realizedRiskPct) +
         ",\"mfe\":" + NullableNumber(rec.mfe) +
         ",\"mae\":" + NullableNumber(rec.mae) +
         ",\"htf_trend\":" + Quote(rec.htfTrend) +
         ",\"zone_percent\":" + NullableNumber(rec.zonePercent) +
         ",\"in_ote\":" + NullableFlag(rec.inOte) +
         ",\"zone_types\":" + Quote(rec.zoneTypes) +
         ",\"trigger\":" + Quote(rec.trigger) +
         ",\"session\":" + Quote(rec.session) +
         ",\"confluence_score\":" + Number(rec.confluenceScore) +
         ",\"confluence_count\":" + IntegerToString(rec.confluenceCount) +
         "}";
      return true;
     }

   static void Close(const int handle)
     {
      if(handle != INVALID_HANDLE)
         FileClose(handle);
     }

private:
   // Copied from CSmcSnapshotExporter's private helpers. They are duplicated
   // rather than exposed because widening another class's API to avoid a small
   // copy would change a public contract that AGENTS.md requires be preserved.
   //
   // The copies are verified byte-identical as of this commit but there is NO
   // automated drift check: both are private, so no test can compare them
   // directly. If SnapshotExporter's escaping changes, these must be updated by
   // hand. TestTradeJournal pins the journal side's escaping behaviour.
   static string Quote(const string value)
     {
      string result = "\"";
      for(int i = 0; i < StringLen(value); i++)
        {
         ushort c = StringGetCharacter(value, i);
         if(c == 34) result += "\\\"";
         else if(c == 92) result += "\\\\";
         else if(c < 32 || (c >= 0xD800 && c <= 0xDFFF))
            result += StringFormat("\\u%04x", (uint)c);
         else result += ShortToString(c);
        }
      return result + "\"";
     }

   // 17 significant digits preserve an IEEE-754 double on a JSON round trip.
   static string Number(const double value) { return StringFormat("%.17g", value); }

   // EMPTY_VALUE is the "could not be computed" sentinel. It must serialize as
   // null, never as a number: a reader cannot tell a huge value from a missing
   // one, and 0.0 would assert a measurement that was never made.
   static string NullableNumber(const double value)
     {
      if(value == EMPTY_VALUE) return "null";
      return Number(value);
     }

   static string NullableFlag(const ENUM_SMC_JOURNAL_FLAG value)
     {
      if(value == SMC_JOURNAL_UNSET) return "null";
      return value == SMC_JOURNAL_YES ? "true" : "false";
     }

   // openTime and closeTime are non-nullable in the schema and Validate
   // rejects 0, so unlike CSmcSnapshotExporter's copy this has no null branch.
   static string Timestamp(const datetime value)
     {
      MqlDateTime parts;
      TimeToStruct(value, parts);
      return Quote(StringFormat("%04d-%02d-%02dT%02d:%02d:%02d", parts.year,
                                parts.mon, parts.day, parts.hour, parts.min, parts.sec));
     }

   static bool ValidTime(const datetime value)
     {
      if(value <= 0) return false;
      MqlDateTime parts;
      return TimeToStruct(value, parts) && parts.year >= 1970 && parts.year <= 9999;
     }

   // Rejects records the Python analyzer would reject, so a malformed trade
   // never reaches the journal. Non-finite doubles are caught here because
   // %.17g would render them as "inf" or "nan", which is not valid JSON and
   // would surface downstream as a misattributed parse failure.
   static bool Validate(const SmcTradeRecord &rec)
     {
      if(rec.symbol == "" || rec.timeframe == "" || rec.variant == "" ||
         rec.configHash == "" || rec.exitReason == "" || rec.htfTrend == "" ||
         rec.zoneTypes == "" || rec.trigger == "" || rec.session == "")
         return false;
      if(rec.direction != 1 && rec.direction != -1)
         return false;
      if(!ValidTime(rec.openTime) || !ValidTime(rec.closeTime))
         return false;
      if(rec.closeTime < rec.openTime)
         return false;
      if(rec.barsHeld < 0 || rec.confluenceCount < 0)
         return false;
      if(rec.inOte != SMC_JOURNAL_UNSET && rec.inOte != SMC_JOURNAL_NO &&
         rec.inOte != SMC_JOURNAL_YES)
         return false;

      // Every non-nullable double must be finite. Count must match the
      // initialiser exactly: an off-by-one silently skips a field.
      const double required[17] = {
         rec.lot, rec.entryPrice, rec.exitPrice, rec.sl, rec.tp,
         rec.spreadAtEntry, rec.slippagePoints, rec.grossProfit, rec.commission,
         rec.swap, rec.netProfit, rec.confluenceScore, rec.balanceAtOpen,
         rec.equityAtOpen, rec.marginUsed, rec.intendedRiskPct, rec.realizedRiskPct
      };
      for(int i = 0; i < 17; i++)
         if(!MathIsValidNumber(required[i]))
            return false;

      const double nullable[3] = {rec.mfe, rec.mae, rec.zonePercent};
      for(int i = 0; i < 3; i++)
         if(nullable[i] != EMPTY_VALUE && !MathIsValidNumber(nullable[i]))
            return false;

      if(rec.lot <= 0 || rec.entryPrice <= 0 || rec.exitPrice <= 0)
         return false;
      if(rec.balanceAtOpen <= 0 || rec.equityAtOpen <= 0)
         return false;
      if(rec.sl < 0 || rec.tp < 0 || rec.spreadAtEntry < 0 || rec.marginUsed < 0)
         return false;
      if(rec.intendedRiskPct < 0 || rec.realizedRiskPct < 0)
         return false;
      if(rec.mfe != EMPTY_VALUE && rec.mfe < 0)
         return false;
      if(rec.mae != EMPTY_VALUE && rec.mae > 0)
         return false;
      return true;
     }
  };

#endif // __SMC_TRADE_JOURNAL_MQH__
