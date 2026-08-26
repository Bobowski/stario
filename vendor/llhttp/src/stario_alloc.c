#include "stario_alloc.h"

#include <stdlib.h>
#include <string.h>

llhttp_t* stario_parser_new(void) {
  return (llhttp_t*)calloc(1, sizeof(llhttp_t));
}

void stario_parser_del(llhttp_t* parser) {
  free(parser);
}

llhttp_settings_t* stario_settings_new(void) {
  llhttp_settings_t* settings = (llhttp_settings_t*)calloc(1, sizeof(llhttp_settings_t));
  if (settings != NULL) {
    llhttp_settings_init(settings);
  }
  return settings;
}

void stario_parser_set_data(llhttp_t* parser, void* data) {
  parser->data = data;
}

void* stario_parser_get_data(const llhttp_t* parser) {
  return parser->data;
}

uint16_t stario_parser_flags(const llhttp_t* parser) {
  return parser->flags;
}

uint64_t stario_parser_content_length(const llhttp_t* parser) {
  return parser->content_length;
}
