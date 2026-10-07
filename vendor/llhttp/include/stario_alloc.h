#ifndef STARIO_ALLOC_H
#define STARIO_ALLOC_H

#include "llhttp.h"

llhttp_t* stario_parser_new(void);
void stario_parser_del(llhttp_t* parser);
llhttp_settings_t* stario_settings_new(void);
void stario_settings_del(llhttp_settings_t* settings);
void stario_parser_set_data(llhttp_t* parser, void* data);
void* stario_parser_get_data(const llhttp_t* parser);
uint16_t stario_parser_flags(const llhttp_t* parser);
uint64_t stario_parser_content_length(const llhttp_t* parser);

#endif
