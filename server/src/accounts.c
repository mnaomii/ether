/* Accounts: auto-register on first LOGIN, stored as "nick:hash" lines.
 * Hash = crypt() with a "$6$<salt>" setting (salted SHA-512, link -lcrypt). Never log passwords. */
#include "server.h"
#include <string.h>
#include <fcntl.h>
#include <sys/random.h>
#include <crypt.h>
#include <stdio.h>
#include <errno.h>
#include <stdlib.h>

// ---- helpers


static void make_setting(char out[21]){
    static const char ALPHA[] =
        "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";   // 64 chars
    uint8_t rnd[16];
    getrandom(rnd, sizeof rnd, 0);           // 16 random bytes (0..255 each)

    memcpy(out, "$6$", 3);                   // out[0..2]
    for (int i = 0; i < 16; ++i)
        out[3 + i] = ALPHA[rnd[i] % 64];           // a random byte → one of the 64 chars
    out[19] = '$';
    out[20] = '\0';
}

static int format_check(const char* line){

    // returns 1 == false
    // line may or may not be stripped

    char tmp[strlen(line)+1]; strcpy(tmp, line);
    char delim[] = "\r\n";
    strtok(tmp,delim);

    char* sep = strchr(tmp, ':');

    if(sep == NULL || sep - tmp > MAX_NICK 
        || sep - tmp < 1  || strlen(sep+1) < 1
        || strlen(sep+1) >= HASH_LEN || strchr(sep+1, ':')) 
                                return 1;

    size_t length = strlen(tmp);
    for(size_t i = 0; i < length; ++i){
        unsigned char c = tmp[i];
        if(strchr(":\n",c)) continue;
        if((c < 0x20 && c != '\r') || c == 0x7f) return 1;
    }

    return 0;
}

int accounts_load(struct server *s, const char *path)
{
    s->accounts_path = path;

    FILE* fd = fopen(path, "r");
    if(fd == NULL) { 
        perror("[ERR:accounts_load()] cannot load accounts' file"); 
        return (errno == ENOENT) ? 0 : -1; // file doesnt exist -> not an error
    }

    
    char* line = NULL; size_t cap = 0; 
    ssize_t bytes = getline(&line,&cap,fd);

    while(bytes > -1 && s->n_accounts < MAX_ACCOUNTS){

        if(format_check(line) != 1) {
            char delim[] = ":\r\n";
            char* nick = strtok(line, delim);
            char* hash = strtok(NULL, delim);

        strcpy(s->accounts[s->n_accounts].nick ,nick);
        strcpy(s->accounts[s->n_accounts++].hash, hash);
        }

        bytes = getline(&line,&cap,fd);

    }

    free(line);
    fclose(fd);

    return 0;

}

enum acc_result account_login(struct server *s, const char *nick, const char *pass)
{

    // validating the format (different from the above helper)
    size_t nick_l = strlen(nick), pass_l = strlen(pass);
    if(nick_l < 1 || nick_l > MAX_NICK) return ACC_BAD_FORMAT;
    if(pass_l < 1 || pass_l > MAX_PASS) return ACC_BAD_FORMAT;

    for(int i = 0; (size_t)i < nick_l ; ++i){
        unsigned char c = nick[i];
        if(c == ':' || c < 0x20 || c == 0x7f) return ACC_BAD_FORMAT;
    }

    struct account* acc = NULL;
    for(int i = 0; i < s->n_accounts && !acc; ++i) {
        if(strcmp(s->accounts[i].nick, nick) == 0) acc = &s->accounts[i];
    }

    char *hash = NULL;
    if(acc) { // check pass w/ hash
        // the stored hash starts with "$6$<salt>$": crypt reuses that salt, same pass -> same hash
        hash = crypt(pass, acc->hash);
        if (hash == NULL || hash[0] == '*') { 
            perror("[ERR:account_login()] failed to hash password");  return ACC_BAD_PASS;
        }


        if(strcmp(hash, acc->hash) == 0) return ACC_OK; else return ACC_BAD_PASS;

    } else { // create account and add to file

        if(s->n_accounts == MAX_ACCOUNTS) {
            perror("[ERR:account_login()] maximum nb. accounts reached");
            return ACC_BAD_FORMAT;
        }

        char setting[21]; make_setting(setting);
        hash = crypt(pass,setting);

        if (hash == NULL || hash[0] == '*') { 
            perror("[ERR:account_login()] failed to hash password");  return ACC_BAD_PASS;
        }


        FILE* fd = fopen(s->accounts_path, "a"); // appending to file
        if(fd == NULL) { 
            perror("[ERR:account_login()] cannot create account");
            return ACC_BAD_FORMAT;
        }

        acc = &s->accounts[s->n_accounts++];
        strcpy(acc->nick, nick);
        strcpy(acc->hash, hash); // copy now: crypt returns a static buffer the next call overwrites



        fprintf(fd,"%s:%s\n",nick,hash);
        fclose(fd);
        return ACC_CREATED;
    }

    return ACC_BAD_FORMAT;

}
