#property strict
#include "TestHarness.mqh"
#include <SMC/Utils/TradeJournal.mqh>

// Reads a journal back from FILE_COMMON and asserts it is UTF-8, never UTF-16.
string ReadJournalBytes(const string filename)
  {
   int handle = FileOpen(filename, FILE_READ | FILE_BIN | FILE_COMMON);
   if(handle == INVALID_HANDLE)
      return "";
   uchar bytes[];
   uint count = (uint)FileSize(handle);
   uint copied = FileReadArray(handle, bytes, 0, count);
   FileClose(handle);
   TestEqual(copied, count, "read every journal byte");
   for(uint i = 0; i < count; i++)
      if(bytes[i] == 0)
        {
         TestAssert(false, "journal is UTF-8 without UTF-16 null bytes");
         return "";
        }
   if(count >= 2)
     {
      const bool utf16le = (bytes[0] == 0xFF && bytes[1] == 0xFE);
      const bool utf16be = (bytes[0] == 0xFE && bytes[1] == 0xFF);
      TestAssert(!utf16le && !utf16be, "journal carries no UTF-16 byte order mark");
     }
   if(count >= 3)
      TestAssert(!(bytes[0] == 0xEF && bytes[1] == 0xBB && bytes[2] == 0xBF),
                 "journal carries no UTF-8 byte order mark");
   return CharArrayToString(bytes, 0, (int)count, CP_UTF8);
  }

int CountLines(const string text)
  {
   if(StringLen(text) == 0)
      return 0;
   int count = 0;
   int pos = 0;
   while((pos = StringFind(text, "\n", pos)) >= 0)
     {
      count++;
      pos++;
     }
   return count;
  }

string JournalName()
  {
   return "smc-test-journal-" + SMC_TestRunId + ".jsonl";
  }

// A fully populated, valid record. Every field is set on purpose: an unset
// string is "" and an unset double is 0, and both would be rejected or
// silently misread, so a passing test must not depend on defaults.
SmcTradeRecord MakeRecord()
  {
   SmcTradeRecord rec;
   rec.ticket = 200001;
   rec.positionId = 200001;
   rec.symbol = "XAUUSD";
   rec.timeframe = "M15";
   rec.magic = 20260209;
   rec.variant = "baseline";
   rec.configHash = CSmcTradeJournal::ConfigHash("InpRiskPercent=1.0|InpMaxSpreadPips=3.0");
   rec.openTime = D'2026.09.21 08:00:00';
   rec.closeTime = D'2026.09.21 10:00:00';
   rec.direction = 1;
   rec.barsHeld = 8;
   rec.lot = 0.01;
   rec.entryPrice = 2650.0;
   rec.sl = 2645.0;
   rec.tp = 2660.0;
   rec.exitPrice = 2660.0;
   rec.exitReason = "tp";
   rec.spreadAtEntry = 2.5;
   rec.slippagePoints = 0.0;
   rec.grossProfit = 10.35;
   rec.commission = -0.35;
   rec.swap = 0.0;
   rec.netProfit = 10.0;
   rec.balanceAtOpen = 500.0;
   rec.equityAtOpen = 500.0;
   rec.marginUsed = 5.3;
   rec.intendedRiskPct = 1.0;
   rec.realizedRiskPct = 0.0;
   rec.mfe = 10.0;
   rec.mae = -1.2;
   rec.htfTrend = "bullish";
   rec.zonePercent = 32.5;
   rec.inOte = SMC_JOURNAL_YES;
   rec.zoneTypes = "OB|IFVG";
   rec.trigger = "displacement";
   rec.session = "London";
   rec.confluenceScore = 0.72;
   rec.confluenceCount = 4;
   return rec;
  }

// Deliberately does NOT clear `line` first. SerializeTrade must clear it even
// when it rejects the record, or a caller reusing one buffer across trades
// would still be holding the previous trade's JSON after a rejection.
bool Serializes(const SmcTradeRecord &rec, string &line)
  {
   return CSmcTradeJournal::SerializeTrade(rec, line);
  }

void OnStart()
  {
   TestBegin("trade-journal");

   TestAssert(CSmcTradeJournal::Version() == "1.0", "journal version matches the schema");

   // --- append semantics: the decisive risk for this PR --------------------
   const string name = JournalName();
   FileDelete(name, FILE_COMMON);

   int handle = INVALID_HANDLE;
   TestAssert(CSmcTradeJournal::Open(name, handle), "open a new journal for append");
   TestAssert(handle != INVALID_HANDLE, "open returns a usable handle");
   TestAssert(CSmcTradeJournal::AppendLine(handle, "{\"n\":1}\n"), "append line 1");
   TestAssert(CSmcTradeJournal::AppendLine(handle, "{\"n\":2}\n"), "append line 2");
   TestAssert(CSmcTradeJournal::AppendLine(handle, "{\"n\":3}\n"), "append line 3");
   CSmcTradeJournal::Close(handle);

   const string first = ReadJournalBytes(name);
   TestEqual(CountLines(first), 3, "three lines survive the first close");

   handle = INVALID_HANDLE;
   TestAssert(CSmcTradeJournal::Open(name, handle), "reopen an existing journal for append");
   TestAssert(CSmcTradeJournal::AppendLine(handle, "{\"n\":4}\n"), "append line 4");
   TestAssert(CSmcTradeJournal::AppendLine(handle, "{\"n\":5}\n"), "append line 5");
   CSmcTradeJournal::Close(handle);

   const string second = ReadJournalBytes(name);
   TestEqual(CountLines(second), 5, "reopen did not truncate the journal");
   TestEqual(StringFind(second, "{\"n\":1}"), 0, "line 1 is still at offset 0");
   TestAssert(StringFind(second, "{\"n\":3}\n{\"n\":4}\n") >= 0,
              "appended lines continue immediately after the previous run's last line");
   TestEqual(StringLen(second), StringLen(first) + StringLen("{\"n\":4}\n") + StringLen("{\"n\":5}\n"),
             "the file grew by exactly the two appended lines");

   // --- boundary behaviour -------------------------------------------------
   TestAssert(!CSmcTradeJournal::Open("../escape.jsonl", handle), "reject a path traversal name");
   TestAssert(handle == INVALID_HANDLE, "a rejected open leaves the handle invalid");
   TestAssert(!CSmcTradeJournal::Open("bad:name.jsonl", handle), "reject a reserved character");
   TestAssert(!CSmcTradeJournal::AppendLine(INVALID_HANDLE, "{\"n\":9}\n"),
              "append on an invalid handle fails instead of writing nowhere");

   FileDelete(name, FILE_COMMON);
   TestAssert(!FileIsExist(name, FILE_COMMON), "the test cleans up its journal");

   // --- AppendTrade end to end ---------------------------------------------
   const SmcTradeRecord tradeRecord = MakeRecord();
   FileDelete(name, FILE_COMMON);
   TestAssert(CSmcTradeJournal::Open(name, handle), "open a journal for trade records");
   TestAssert(CSmcTradeJournal::AppendTrade(handle, tradeRecord), "append a valid trade");
   TestAssert(CSmcTradeJournal::AppendTrade(handle, tradeRecord), "append a second valid trade");
   CSmcTradeJournal::Close(handle);

   const string traded = ReadJournalBytes(name);
   TestEqual(CountLines(traded), 2, "AppendTrade wrote one newline-terminated line per trade");
   FileDelete(name, FILE_COMMON);

   // --- serialization contract ---------------------------------------------
   string line;
   TestAssert(Serializes(tradeRecord, line), "a fully populated record serializes");
   TestAssert(StringFind(line, "{\"journal_version\":\"1.0\",") == 0,
              "the line opens with the version key");
   TestEqual(StringFind(line, "\n"), -1, "SerializeTrade adds no trailing newline");

   // All 39 keys, in schema order. Checked by name so a rename breaks loudly.
   const string keys[39] = {"journal_version", "ticket", "position_id", "symbol", "timeframe",
                            "magic", "variant", "config_hash", "open_time", "close_time",
                            "direction", "bars_held", "lot", "entry_price", "sl", "tp",
                            "exit_price", "exit_reason", "spread_at_entry", "slippage_points",
                            "gross_profit", "commission", "swap", "net_profit",
                            "balance_at_open", "equity_at_open", "margin_used",
                            "intended_risk_pct", "realized_risk_pct", "mfe", "mae",
                            "htf_trend", "zone_percent", "in_ote", "zone_types", "trigger",
                            "session", "confluence_score", "confluence_count"};
   int previous = -1;
   for(int i = 0; i < 39; i++)
     {
      const int at = StringFind(line, "\"" + keys[i] + "\":");
      TestAssert(at >= 0, "key present: " + keys[i]);
      TestAssert(at > previous, "key in schema order: " + keys[i]);
      previous = at;
     }

   TestAssert(StringFind(line, "\"open_time\":\"2026-09-21T08:00:00\"") >= 0,
              "timestamps are broker-local without a UTC suffix");
   TestAssert(StringFind(line, "\"in_ote\":true") >= 0, "a known flag serializes as a JSON boolean");
   TestAssert(StringFind(line, "\"zone_types\":\"OB|IFVG\"") >= 0, "a pipe inside a string is not escaped");

   // --- nullable fields: null must never become a plausible zero ------------
   SmcTradeRecord unknownContext = MakeRecord();
   unknownContext.mfe = EMPTY_VALUE;
   unknownContext.mae = EMPTY_VALUE;
   unknownContext.zonePercent = EMPTY_VALUE;
   unknownContext.inOte = SMC_JOURNAL_UNSET;
   TestAssert(Serializes(unknownContext, line), "a record with uncomputed context serializes");
   TestAssert(StringFind(line, "\"mfe\":null") >= 0, "an uncomputed mfe is null, not 0");
   TestAssert(StringFind(line, "\"mae\":null") >= 0, "an uncomputed mae is null, not 0");
   TestAssert(StringFind(line, "\"zone_percent\":null") >= 0, "an uncomputed zone_percent is null");
   TestAssert(StringFind(line, "\"in_ote\":null") >= 0, "an unevaluated in_ote is null, not false");

   SmcTradeRecord evaluated = MakeRecord();
   evaluated.inOte = SMC_JOURNAL_NO;
   evaluated.mfe = 0.0;
   TestAssert(Serializes(evaluated, line), "a record with real zero measurements serializes");
   TestAssert(StringFind(line, "\"in_ote\":false") >= 0, "an evaluated false stays false");
   TestAssert(StringFind(line, "\"mfe\":0") >= 0, "a measured zero stays a number");

   // --- escaping ------------------------------------------------------------
   SmcTradeRecord nasty = MakeRecord();
   nasty.symbol = "a\"b\\c" + ShortToString(1) + ShortToString(0x4E2D);
   TestAssert(Serializes(nasty, line), "a record with hostile text serializes");
   TestAssert(StringFind(line, "a\\\"b\\\\c") >= 0, "quotes and backslashes are escaped");
   TestAssert(StringFind(line, "\\u0001") >= 0, "a control character is escaped as \\u0001");
   // Checked on the in-memory string, before StringToCharArray. The record is
   // not written to disk here, so this pins the escaping decision rather than
   // the UTF-8 encoding of non-ASCII bytes; the ASCII path through
   // StringToCharArray is what ReadJournalBytes proves end to end.
   TestAssert(StringFind(line, ShortToString(0x4E2D)) >= 0,
              "non-ASCII text survives escaping unescaped");

   // --- numeric precision ---------------------------------------------------
   SmcTradeRecord precise = MakeRecord();
   precise.netProfit = 0.1 + 0.2;
   TestAssert(Serializes(precise, line), "a record with an inexact double serializes");
   TestAssert(StringFind(line, "0.30000000000000004") >= 0,
              "doubles keep 17 significant digits rather than a lossy rounding");

   // --- ConfigHash ----------------------------------------------------------
   const string hashA = CSmcTradeJournal::ConfigHash("InpRiskPercent=1.0");
   const string hashB = CSmcTradeJournal::ConfigHash("InpRiskPercent=1.0");
   const string hashC = CSmcTradeJournal::ConfigHash("InpRiskPercent=2.0");
   TestAssert(hashA == hashB, "ConfigHash is deterministic");
   TestAssert(hashA != hashC, "ConfigHash distinguishes different parameters");
   TestEqual(StringLen(hashA), 16, "ConfigHash is 16 hex characters");
   TestEqual(StringLen(CSmcTradeJournal::ConfigHash("")), 16, "an empty input still hashes");
   // A length check cannot catch a truncated hash: %016x on a 32-bit read still
   // prints 16 characters, just with eight leading zeros. Pin the high word.
   TestAssert(StringSubstr(hashA, 0, 8) != "00000000",
              "ConfigHash uses the full 64 bits, not only the low word");
   TestAssert(StringSubstr(CSmcTradeJournal::ConfigHash(""), 0, 8) != "00000000",
              "even the empty input exercises the high word");

   // --- validation rejects what the analyzer would reject -------------------
   SmcTradeRecord bad;
   bad = MakeRecord(); bad.closeTime = D'2026.09.21 07:00:00';
   TestAssert(!Serializes(bad, line), "reject a close before the open");
   bad = MakeRecord(); bad.lot = 0.0;
   TestAssert(!Serializes(bad, line), "reject a zero lot");
   bad = MakeRecord(); bad.entryPrice = -1.0;
   TestAssert(!Serializes(bad, line), "reject a non-positive entry price");
   bad = MakeRecord(); bad.direction = 0;
   TestAssert(!Serializes(bad, line), "reject a direction that is neither long nor short");
   bad = MakeRecord(); bad.symbol = "";
   TestAssert(!Serializes(bad, line), "reject an empty required string");
   bad = MakeRecord(); bad.exitReason = "invented";
   TestAssert(Serializes(bad, line), "exit_reason is a free string here; the schema owns the enum");
   bad = MakeRecord(); bad.netProfit = MathPow(10, 400);
   TestAssert(!Serializes(bad, line), "reject a non-finite double that %.17g would render as inf");
   bad = MakeRecord(); bad.mfe = -1.0;
   TestAssert(!Serializes(bad, line), "reject a negative mfe");
   bad = MakeRecord(); bad.mae = 1.0;
   TestAssert(!Serializes(bad, line), "reject a positive mae");
   bad = MakeRecord(); bad.openTime = 0;
   TestAssert(!Serializes(bad, line), "reject a missing open time");

   // --- rejection must clear the buffer and write nothing --------------------
   string buffer;
   TestAssert(Serializes(tradeRecord, buffer), "a valid record fills the buffer");
   TestAssert(StringLen(buffer) > 0, "the buffer holds the serialization");
   SmcTradeRecord invalidTrade = MakeRecord();
   invalidTrade.lot = 0.0;
   TestAssert(!Serializes(invalidTrade, buffer), "an invalid record is rejected");
   TestEqual(StringLen(buffer), 0,
             "rejection clears the buffer instead of leaving the previous trade in it");

   FileDelete(name, FILE_COMMON);
   TestAssert(CSmcTradeJournal::Open(name, handle), "open a journal for the rejection test");
   TestAssert(CSmcTradeJournal::AppendTrade(handle, tradeRecord), "append the valid trade first");
   TestAssert(!CSmcTradeJournal::AppendTrade(handle, invalidTrade),
              "AppendTrade refuses a record the analyzer would reject");
   CSmcTradeJournal::Close(handle);
   TestEqual(CountLines(ReadJournalBytes(name)), 1,
             "a refused record wrote no bytes and left no partial line");
   TestAssert(!CSmcTradeJournal::AppendLine(handle, "{\"n\":1}\n"),
              "append after Close fails on the stale handle instead of writing nowhere");
   FileDelete(name, FILE_COMMON);

   TestAssert(!CSmcTradeJournal::Open("", handle), "reject an empty filename");
   TestAssert(handle == INVALID_HANDLE, "a rejected empty name leaves the handle invalid");

   // --- an unpopulated record must not fabricate measurements ---------------
   // The constructor, not MQL5's zero-initialisation, is what makes "not
   // computed" the default. Without it a PR 3 caller that misses mfe on a
   // CopyRates failure would journal 0.0, meaning "price never moved
   // favourably", and Validate would accept it.
   SmcTradeRecord sparse;
   sparse.ticket = 1;
   sparse.positionId = 1;
   sparse.symbol = "XAUUSD";
   sparse.timeframe = "M15";
   sparse.magic = 1;
   sparse.variant = "sparse";
   sparse.configHash = "0000000000000000";
   sparse.openTime = D'2026.09.21 08:00:00';
   sparse.closeTime = D'2026.09.21 09:00:00';
   sparse.direction = 1;
   sparse.lot = 0.01;
   sparse.entryPrice = 2650.0;
   sparse.exitPrice = 2655.0;
   sparse.exitReason = "signal";
   sparse.balanceAtOpen = 500.0;
   sparse.equityAtOpen = 500.0;
   sparse.htfTrend = "unknown";
   sparse.zoneTypes = "unknown";
   sparse.trigger = "unknown";
   sparse.session = "unknown";
   string sparseJson;
   TestAssert(Serializes(sparse, sparseJson), "a sparsely populated record serializes");
   TestAssert(StringFind(sparseJson, "\"mfe\":null") >= 0, "an unset mfe is null, not 0");
   TestAssert(StringFind(sparseJson, "\"mae\":null") >= 0, "an unset mae is null, not 0");
   TestAssert(StringFind(sparseJson, "\"zone_percent\":null") >= 0,
              "an unset zone_percent is null, not 0");
   TestAssert(StringFind(sparseJson, "\"in_ote\":null") >= 0,
              "an unset in_ote is null, not false");

   // --- schema-legal enum tokens --------------------------------------------
   // CSmcKillZone::GetSessionName is deliberately NOT used to fill `session`:
   // it returns "New York", "LDN-NY Overlap" and "None", three of five values
   // the schema rejects. These tokens are the contract; the Python suite pins
   // the same set against schemas/trade-journal.schema.json.
   TestAssert(CSmcTradeJournal::SessionToken(SESSION_NONE) == "none", "SESSION_NONE token");
   TestAssert(CSmcTradeJournal::SessionToken(SESSION_ASIAN) == "Asian", "SESSION_ASIAN token");
   TestAssert(CSmcTradeJournal::SessionToken(SESSION_LONDON) == "London", "SESSION_LONDON token");
   TestAssert(CSmcTradeJournal::SessionToken(SESSION_NEWYORK) == "NewYork",
              "SESSION_NEWYORK token has no space, unlike GetSessionName");
   TestAssert(CSmcTradeJournal::SessionToken(SESSION_LDN_NY_OL) == "ldn_ny_overlap",
              "SESSION_LDN_NY_OL has its own token rather than borrowing London or NewYork");
   TestAssert(CSmcTradeJournal::SessionToken((ENUM_SMC_SESSION)99) == "unknown",
              "an out-of-range session degrades to unknown");
   TestAssert(CSmcTradeJournal::TrendToken(SMC_TREND_BULLISH) == "bullish", "bullish token");
   TestAssert(CSmcTradeJournal::TrendToken(SMC_TREND_BEARISH) == "bearish", "bearish token");
   TestAssert(CSmcTradeJournal::TrendToken(SMC_TREND_RANGING) == "ranging",
              "ranging, not the neutral that SmcDirectionName returns");
   TestAssert(CSmcTradeJournal::TrendToken((ENUM_SMC_TREND)99) == "unknown",
              "an out-of-range trend degrades to unknown");

   // --- run filename ---------------------------------------------------------
   const string runName = CSmcTradeJournal::JournalFilename(20260209);
   TestAssert(StringFind(runName, "trade-journal-20260209-") == 0,
              "the run filename carries the magic number");
   TestAssert(StringFind(runName, ".jsonl") == StringLen(runName) - 6,
              "the run filename has the journal extension");
   TestAssert(CSmcSnapshotExporter::SafePath(runName), "the generated name is safe to open");

   // Byte-level contract, pinned jointly with
   // tests/fixtures/trade-journal-v1-mql5.jsonl. That fixture is the captured
   // output of exactly these two records and is loaded by the Python analyzer,
   // so changing serialization here without re-capturing there lets the two
   // sides drift apart silently.
   //
   // To re-capture: assert against a placeholder and read the real bytes from
   // the failure label, which the runner prints into the terminal log whenever
   // a fixture fails. On success the log is suppressed, so Print() cannot be
   // used to retrieve them.
   string baselineJson;
   Serializes(tradeRecord, baselineJson);
   string nullsJson;
   Serializes(unknownContext, nullsJson);
   TestEqual(StringLen(baselineJson), 798, "baseline serialization matches the captured length");
   TestEqual(StringLen(nullsJson), 800, "nullable serialization matches the captured length");
   TestAssert(StringFind(baselineJson, "\"config_hash\":\"ad40da8852c20a64\"") >= 0,
              "config_hash matches the captured fixture");
   // Pins the exact binary64 spelling of the captured bytes. Note this does NOT
   // prove 17 digits are needed for the P&L identity: the analyzer allows a
   // one-cent tolerance, so %.2f would also satisfy it. The real precision pin
   // is the 0.1 + 0.2 case above, which no shorter format can survive.
   TestAssert(StringFind(baselineJson, "\"commission\":-0.34999999999999998") >= 0,
              "commission keeps its exact binary64 spelling");

   TestFinish();
  }
