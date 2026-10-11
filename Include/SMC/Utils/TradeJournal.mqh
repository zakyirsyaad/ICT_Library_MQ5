//+------------------------------------------------------------------+
//|                                               TradeJournal.mqh   |
//|                     Append-only JSON Lines writer for trades     |
//+------------------------------------------------------------------+
#ifndef __SMC_TRADE_JOURNAL_MQH__
#define __SMC_TRADE_JOURNAL_MQH__

#include <SMC/Utils/SnapshotExporter.mqh>

// Writes one closed trade per line to a JSON Lines journal under FILE_COMMON.
//
// Encoding: FILE_BIN with an explicit CP_UTF8 char array, matching
// CSmcSnapshotExporter. FILE_TXT alone is NOT UTF-8 in MQL5 -- without
// FILE_ANSI it uses the system ANSI codepage. FILE_UNICODE must never be
// used here: it produces UTF-16 with a BOM, which the Python analyzer
// rejects outright as JournalFormatError.
class CSmcTradeJournal
  {
public:
   static string Version()
     {
      return "1.0";
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
      // file as UTF-16. Writing the CP_UTF8 array is what CSmcSnapshotExporter
      // does, and it is the only form that produces UTF-8 without a BOM.
      const uint written = FileWriteArray(handle, bytes, 0, count);
      if(written != (uint)count || GetLastError() != 0)
         return false;
      FileFlush(handle);
      return true;
     }

   static void Close(const int handle)
     {
      if(handle != INVALID_HANDLE)
         FileClose(handle);
     }
  };

#endif // __SMC_TRADE_JOURNAL_MQH__
