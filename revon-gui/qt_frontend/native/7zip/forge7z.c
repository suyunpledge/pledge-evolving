/* revon bridge to the public-domain 7-Zip ANSI-C decoder. */
#include <stddef.h>
#include <stdlib.h>
#include <stdint.h>

#include "7z.h"
#include "7zCrc.h"
#include "7zFile.h"

#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

typedef int (*Running)(void);
typedef union {
    size_t size;
    max_align_t alignment;
} Allocation;

typedef struct {
    ISzAlloc api;
    size_t used, limit;
} Budget;

typedef struct {
    ISeekInStream api;
    CSzFile file;
    Running running;
} Input;

typedef struct {
    Input input;
    CLookToRead2 look;
    Byte read_buffer[1 << 18];
    CSzArEx archive;
    Budget budget;
    Byte *decoded;
    size_t decoded_size;
    UInt32 block;
} Reader;

static void *allocate(ISzAllocPtr api, size_t size)
{
    Budget *budget = (Budget *)api;
    Allocation *p;
    if (!size || size > budget->limit - budget->used ||
        size > SIZE_MAX - sizeof(Allocation))
        return NULL;
    p = (Allocation *)malloc(sizeof(Allocation) + size);
    if (!p)
        return NULL;
    p->size = size;
    budget->used += size;
    return p + 1;
}

static void release(ISzAllocPtr api, void *pointer)
{
    Budget *budget = (Budget *)api;
    if (pointer) {
        Allocation *p = (Allocation *)pointer - 1;
        budget->used -= p->size;
        free(p);
    }
}

static SRes read_input(ISeekInStreamPtr api, void *buf, size_t *size)
{
    Input *input = (Input *)api;
    if (!input->running())
        return SZ_ERROR_PROGRESS;
    return File_Read(&input->file, buf, size) ? SZ_ERROR_READ : SZ_OK;
}

static SRes seek_input(ISeekInStreamPtr api, Int64 *position, ESzSeek origin)
{
    Input *input = (Input *)api;
    if (!input->running())
        return SZ_ERROR_PROGRESS;
    return File_Seek(&input->file, position, origin) ? SZ_ERROR_READ : SZ_OK;
}

EXPORT void forge7z_crc_init(void)
{
    CrcGenerateTable();
}

EXPORT void forge7z_close(Reader *reader)
{
    if (!reader)
        return;
    release(&reader->budget.api, reader->decoded);
    SzArEx_Free(&reader->archive, &reader->budget.api);
    File_Close(&reader->input.file);
    free(reader);
}

EXPORT Reader *forge7z_open(const void *path, size_t memory_limit, Running running, int *error)
{
    Reader *reader = (Reader *)calloc(1, sizeof(Reader));
    if (!reader) {
        *error = SZ_ERROR_MEM;
        return NULL;
    }

    reader->input.api.Read = read_input;
    reader->input.api.Seek = seek_input;
    reader->input.running = running;
    File_Construct(&reader->input.file);
#ifdef _WIN32
    if (InFile_OpenW(&reader->input.file, path)) {
#else
    if (InFile_Open(&reader->input.file, path)) {
#endif
        *error = SZ_ERROR_READ;
        free(reader);
        return NULL;
    }

    reader->budget.api.Alloc = allocate;
    reader->budget.api.Free = release;
    reader->budget.limit = memory_limit;
    reader->block = (UInt32)-1;
    reader->look.realStream = &reader->input.api;
    reader->look.buf = reader->read_buffer;
    reader->look.bufSize = sizeof(reader->read_buffer);
    LookToRead2_CreateVTable(&reader->look, False);
    LookToRead2_INIT(&reader->look);
    SzArEx_Init(&reader->archive);
    *error = SzArEx_Open(&reader->archive, &reader->look.vt,
                       &reader->budget.api, &reader->budget.api);
    if (*error != SZ_OK) {
        forge7z_close(reader);
        return NULL;
    }
    return reader;
}

EXPORT uint32_t forge7z_count(const Reader *reader)
{
    return reader->archive.NumFiles;
}

EXPORT size_t forge7z_entry(const Reader *reader, uint32_t index, uint16_t *name,
                            size_t name_capacity, uint64_t *size, uint32_t *flags)
{
    const CSzArEx *archive = &reader->archive;
    size_t length;
    UInt32 attributes = 0;
    if (index >= archive->NumFiles)
        return 0;
    length = SzArEx_GetFileNameUtf16(archive, index, NULL);
    if (name && length <= name_capacity)
        SzArEx_GetFileNameUtf16(archive, index, name);
    *size = SzArEx_GetFileSize(archive, index);
    if (SzBitWithVals_Check(&archive->Attribs, index))
        attributes = archive->Attribs.Vals[index];
    *flags = SzArEx_IsDir(archive, index) ? 1 : 0;
    /* Preserve no reparse points or non-regular POSIX file types. */
    if ((attributes & 0x400) ||
        ((attributes & 0x8000) && ((attributes >> 16) & 0170000) != 0 &&
         ((attributes >> 16) & 0170000) != 0100000 &&
         ((attributes >> 16) & 0170000) != 0040000))
        *flags |= 2;
    return length;
}

EXPORT int forge7z_bytes(Reader *reader, uint32_t index, const void **data, size_t *size)
{
    size_t offset = 0;
    int result;
    if (index >= reader->archive.NumFiles)
        return SZ_ERROR_PARAM;
    if (!reader->input.running())
        return SZ_ERROR_PROGRESS;
    result = SzArEx_Extract(&reader->archive, &reader->look.vt, index,
                            &reader->block, &reader->decoded, &reader->decoded_size,
                            &offset, size, &reader->budget.api, &reader->budget.api);
    if (result == SZ_OK)
        *data = *size ? reader->decoded + offset : NULL;
    return result;
}
