package main

import (
	"flag"
	"fmt"
	"log"
	"os"
	"runtime"
)

func main() {
	impl := flag.String("impl", envOr("BENCH_IMPL", "nethttp"), "http stack: nethttp or fasthttp")
	host := flag.String("host", envOr("BENCH_HOST", "127.0.0.1"), "bind host")
	port := flag.Int("port", atoiDefault(os.Getenv("BENCH_PORT"), 3000), "bind port")
	flag.Parse()

	if n := os.Getenv("GOMAXPROCS"); n != "" {
		log.Printf("go %s impl=%s GOMAXPROCS=%s (runtime=%d)", runtime.Version(), *impl, n, runtime.GOMAXPROCS(0))
	} else {
		log.Printf("go %s impl=%s GOMAXPROCS=%d", runtime.Version(), *impl, runtime.GOMAXPROCS(0))
	}

	addr := fmt.Sprintf("%s:%d", *host, *port)
	var err error
	switch *impl {
	case "nethttp", "net/http", "stdlib":
		err = serveNetHTTP(addr)
	case "fasthttp":
		err = serveFastHTTP(addr)
	default:
		log.Fatalf("unknown -impl %q (use nethttp or fasthttp)", *impl)
	}
	if err != nil {
		log.Fatal(err)
	}
}

func envOr(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}
