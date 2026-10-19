CFLAGS ?= -std=gnu11 -Wall -Wextra -g
LDLIBS = -lcrypt

SRC = src/main.c src/buf.c src/tcp.c src/udp.c src/files.c src/accounts.c
HDR = $(wildcard src/*.h)

ether-server: $(SRC) $(HDR)
	$(CC) $(CFLAGS) -o $@ $(SRC) $(LDLIBS)

test_vectors: tests/test_vectors.c src/buf.c src/buf.h src/proto.h
	$(CC) $(CFLAGS) -Isrc -o $@ tests/test_vectors.c src/buf.c

test: test_vectors
	./test_vectors ../shared/vectors.txt

clean:
	rm -f ether-server test_vectors

.PHONY: test clean
