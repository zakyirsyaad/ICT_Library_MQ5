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

void OnStart()
  {
   TestBegin("trade-journal");

   TestAssert(CSmcTradeJournal::Version() == "1.0", "journal version matches the schema");

   const string name = JournalName();
   FileDelete(name, FILE_COMMON);

   // --- the decisive test: appending must not truncate -------------------
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

   // --- boundary behaviour ------------------------------------------------
   TestAssert(!CSmcTradeJournal::Open("../escape.jsonl", handle), "reject a path traversal name");
   TestAssert(handle == INVALID_HANDLE, "a rejected open leaves the handle invalid");
   TestAssert(!CSmcTradeJournal::Open("bad:name.jsonl", handle), "reject a reserved character");
   TestAssert(!CSmcTradeJournal::AppendLine(INVALID_HANDLE, "{\"n\":9}\n"),
              "append on an invalid handle fails instead of writing nowhere");

   FileDelete(name, FILE_COMMON);
   TestAssert(!FileIsExist(name, FILE_COMMON), "the test cleans up its journal");

   TestFinish();
  }
