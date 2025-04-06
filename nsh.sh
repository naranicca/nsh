#!/usr/bin/env bash

##############################################################################
# configs
NSH_DEFAULT_CONFIG="# nsh preferences
HISTSIZE=1000
NSH_MENU_HEIGHT=20%
NSH_SHOW_HIDDEN_FILES=0
NSH_PROMPT_PREFIX='echo nsh' # this could be a string, a variable, or even a function, e.g. date
NSH_PROMPT=$'\e[31m>\e[33m>\e[32m>\e[0m'

# default editor
NSH_DEFAULT_EDITOR=vi

# colors
NSH_COLOR_TXT=$'\e[37m'
NSH_COLOR_CMD=$'\e[32m'
NSH_COLOR_VAR=$'\e[36m'
NSH_COLOR_VAL=$'\e[33m'
NSH_COLOR_ERR=$'\e[31m'
NSH_COLOR_DIR=$'\e[94m'
NSH_COLOR_EXE=$'\e[32m'
NSH_COLOR_IMG=$'\e[95m'
NSH_COLOR_LNK=$'\e[96m'

# aliases
alias ls='command ls --color=auto'
"
eval "$NSH_DEFAULT_CONFIG"

nsh_print_prompt() {
    local NSH_PROMPT_SEPARATOR='\xee\x82\xb0'
    local git_color
    local prefix="\e[0;32;40m$(eval "$NSH_PROMPT_PREFIX" 2>/dev/null || echo "$NSH_PROMPT_PREFIX")"
    prefix="$prefix"$'\e[7m'"$NSH_COLOR_DIR$NSH_PROMPT_SEPARATOR"
    IFS=$'\n' read -d '' __GIT_STAT__ git_color __GIT_CHANGES__ < <(git_status)
    if [[ -z $__GIT_STAT__ ]]; then
        echo -ne "$prefix\e[0;7m$NSH_COLOR_DIR $(dirs) \e[0m$NSH_COLOR_DIR$NSH_PROMPT_SEPARATOR\e[0m "
    else
        local c2=$((git_color+10))
        echo -ne "$prefix\e[0;7m$NSH_COLOR_DIR $(dirs) \e[0m$NSH_COLOR_DIR\e[${c2}m$NSH_PROMPT_SEPARATOR\e[30;${c2}m$__GIT_STAT__\e[0;${git_color}m$NSH_PROMPT_SEPARATOR\e[0m "
    fi
}

NSH_DEFAULT_CONFIG="$NSH_DEFAULT_CONFIG"$'\n'"# functions"
NSH_DEFAULT_CONFIG="$NSH_DEFAULT_CONFIG"$'\n'"$(type nsh_print_prompt | sed 1d)"

##############################################################################
# utility functions
hide_cursor() {
    printf '\e[?25l'
}

show_cursor() {
    printf '\e[?25h'
}

get_terminal_size() {
    IFS=\  read -r LINES COLUMNS < <(stty size)
}

# CAUTION: index starts at 1 not 0
move_cursor() {
    [[ $1 == -* ]] && printf '\e[%sH' "$(($(get_cursor_row)+$1))" || printf '\e[%sH' "$1"
}

save_cursor_pos() {
    printf '\e[7'
}

restore_cursor_pos() {
    printf '\e[8'
}

get_cursor_pos() {
    while true; do
        IFS=';' read -sdR -p $'\E[6n' __ROW__ __COL__ </dev/tty; __ROW__=${__ROW__#*[};
        [[ $__ROW__ =~ ^[0-9]*$ ]] && return # sometimes ROW has weird values
    done
}

get_cursor_row() {
    IFS=';' read -sdR -p $'\E[6n' __ROW__ __COL__ </dev/tty; echo ${__ROW__#*[};
}

get_cursor_col() {
    IFS=';' read -sdR -p $'\E[6n' __ROW__ __COL__ </dev/tty; echo $__COL__;
}

get_timestamp() {
    if [[ "$OSTYPE" == darwin* ]]; then
        echo $(($(date -u +%s) * 1000))
    else
        echo $(($(date +%s%N) / 1000000))
    fi
}

enable_echo() {
    stty echo
}

disable_echo() {
    stty -echo
}

enable_line_wrapping() {
    printf '\e[?7h'
}

disable_line_wrapping() {
    printf '\e[?7l'
}

open_screen() {
    printf '\e[?1049h' # alternative screen buffer
    printf '\e[2J' # clear screen
    # set the scrolling area and move to (0, 0)
    printf '\e[%sr' "${1:-1;$LINES}"
}

close_screen() {
    printf '\e[2J' # clear the terminal
    printf '\e[;r' # reset the scroll region
    printf '\e[?1049l' # restore main screen buffer
}

# since alias doesn't work in the script, define a function with the same name
(return 0 2>/dev/null) || alias() {
    local l="$@"
    local n="${l%%=*}"
    local c="${l#*=}"
    [[ $c != *\{\}* ]] && c="$c {}"
    if [[ $c == \'* || $c == \" ]]; then
        [[ $c != *"${c:0:1}" ]] && echo "unmatched quote" >&2 && return 1
        c="${c:1:$((${#c}-2))}"
    fi
    #eval "$n() { $c \"\$@\"; }"
    eval "$n() { ${c/\{\}/\"\$@\"}; }"
}

strlen() {
    local nbyte nchar str oLang=$LANG oLcAll=$LC_ALL
    LANG=C LC_ALL=C
    nbyte=${#1}
    LANG=$oLang LC_ALL=$oLcAll
    nchar=${#1}
    echo $(((nbyte-nchar)/2+nchar))
}

strip_escape() {
    if [[ $# -eq 0 ]]; then
        while read line; do
            strip_escape "$line"
        done
    else
        sed 's/\x1b\[[0-9;]*[mK]//g' <<< "$@"
    fi
}

strip_spaces() {
    sed -e 's/^[ ]*//g' -e 's/[ ]*$//' <<< "$1"
}

pipe_context() {
    local c='>>'
    [[ -t 0 ]] && c='->'
    [[ -t 1 ]] && c="${c%?}-"
    echo "$c"
}

get_num_cpu() {
    (grep 'physical id' /proc/cpuinfo 2>/dev/null | wc -l 2>/dev/null) || echo 1
}

get_key() {
    _key=
    local k
    local param=''
    while [ $# -gt 1 ]; do
        param="$param $1"
        shift
    done
    [[ "$__eps_get_key__" == 1 && "$param" == *-t\ 0\.* ]] && printf -v "${1:-_key}" "%s" "$_key" && return
    IFS= read -srn 1 $param _key 2>/dev/null
    __ret=$?
    if [[ $__ret -eq 0 && "$_key" == '' ]]; then
        _key=$'\n'
    elif [[ "$_key" == $'\e' ]]; then
        while IFS= read -sn 1 -t $__eps_get_key__ k; do
            _key=$_key$k
            case $k in
                $'\e')
                    _key=$'\e'
                    break
                    ;;
                [a-zA-NP-Z~])
                    break
                    ;;
            esac
        done
    fi
    printf -v "${1:-_key}" "%s" "$_key"
    return $__ret
}
__eps_get_key__=0.1
read -sn 1 -t $__eps_get_key__ _key &>/dev/null
[[ $? -ne 142 ]] && __eps_get_key__=1

fuzzy_word() {
    local p= && [[ $1 == -n ]] && p='-n' && shift
    if [[ $1 == *\"* ]]; then
        local word="$1"
        local cur="${word%%\"*}"
        fuzzy_word -n "$cur"
        word="${word:$((${#cur}+1))}"
        cur="${word%%\"*}"
        echo -n "$cur"
        word="${word:$((${#cur}+1))}"
        fuzzy_word "$word"
    else
        if [[ $1 == *$ ]]; then
            echo $p "${1:-*}*" | sed -e 's/[^.^~^/^*]/*&*/g' -e 's/\*\*/\*/g' -e 's/[\*]*\$[\*]*$//'
        else
            echo $p "${1:-*}*" | sed -e 's/[^.^~^/^*]/*&*/g' -e 's/\*\*/\*/g'
        fi
    fi
}

put_filecolor() {
    local f="${1/#\~\//$HOME\/}"
    if [[ -h "${f%/}" ]]; then
        echo "$NSH_COLOR_LNK"
    elif [[ -d "$f" ]]; then
        echo "$NSH_COLOR_DIR"
    elif [[ -x "$f" ]]; then
        echo "$NSH_COLOR_EXE"
    fi
}

menu() {
    local list disp colors markers selected list_size
    local list_org disp_org colors_org markers_org
    local item trail
    local len w=0
    local cols rows max_cols max_rows c r i j
    local x=0 y=0 icol=0 irow=0 idx x_old
    local wcparam=-L && [[ "$(wc -L <<< "가나다" 2>/dev/null)" != 6 ]] && wcparam=-c
    local color_func marker_func initial=0
    local return_key=() return_fn=() keys
    local avail_rows col0
    local can_select=
    local show_footer=1
    local allow_escape=0
    local search
    local resized=0
    local display=0

    hide_cursor >&2
    disable_echo >&2 </dev/tty
    get_terminal_size </dev/tty
    get_cursor_pos </dev/tty
    col0=$__COL__ && [[ $col0 -gt 1 ]] && echo >&2
    max_rows=$NSH_MENU_HEIGHT
    avail_rows=$((LINES-__ROW__+1))
    can_select_all() { return 0; }

    disable_line_wrapping >&2
    while [[ $# -gt 0 ]]; do
        if [[ $1 == --color-func ]]; then
            color_func="$2"
            shift
        elif [[ $1 == -r || $1 == --max-rows ]]; then
            max_rows=$2
            avail_rows=$2
            shift
        elif [[ $1 == -c || $1 == --max-cols ]]; then
            max_cols=$2
            shift
        elif [[ $1 == --initial ]]; then
            initial=$2
            shift
        elif [[ $1 == --select ]]; then
            can_select=can_select_all
        elif [[ $1 == --can-select ]]; then
            if [[ $(type -t "$2") == function ]]; then
                can_select="$2"
            else
                eval "TEMPSELFUNC() { $2; }" >&2
            fi
            shift
        elif [[ $1 == --marker-func ]]; then
            marker_func="$2"
            shift
        elif [[ $1 == --key ]]; then
            shift && item="$1" && [[ $item == $'\e' ]] && item=$'\e '
            return_key+=("$item")
            shift && return_fn+=("$1") # if fn ends with '...', menu will not end after running the function
        elif [[ $1 == --no-footer ]]; then
            show_footer=0
        elif [[ $1 == --raw ]]; then
            allow_escape=1
        elif [[ $1 == --display ]]; then
            display=1
            x=-1 y=-1
        else
            item="${1//\\n/}"
            [[ -n "$item" ]] && list+=("$item")
        fi
        shift
    done
    if [[ $(pipe_context) == \>* ]]; then
        while IFS= read line; do
            [[ -n $line ]] && list+=("$line")
        done
    fi
    list_size=${#list[@]}
    [[ $list_size -eq 0 ]] && return 0
    colors=() markers=() selected=()
    if [[ -n $color_func ]]; then
        for ((i=0; i<list_size; i++)); do
            colors[$i]="$($color_func "${list[$i]}" "$i")"
        done
    fi
    if [[ -n $marker_func ]]; then
        local marker_exists=0
        for ((i=0; i<list_size; i++)); do
            markers[$i]="$($marker_func "${list[$i]}")"
            [[ -n ${markers[$i]} ]] && marker_exists=1
        done
        if [[ $marker_exists -ne 0 ]]; then
            for ((i=0; i<list_size; i++)); do
                [[ -z ${markers[$i]} ]] && markers[$i]=' '
            done
        fi
    fi

    [[ $max_rows == *% ]] && max_rows=$((LINES*${max_rows%?}/100))
    if [[ $max_rows -lt $avail_rows || # when we have plenty of empty rows below the cursor
          $avail_rows -gt 1 ]]; then   # or we don't need to add empty rows
        max_rows=$avail_rows
    fi
    [[ $list_size -le $max_rows ]] && max_cols=1

    disp=()
    if [[ $list_size -lt 100 && ${max_cols:-100} -gt 1 ]]; then
        for ((i=0; i<list_size; i++)); do
            disp[$i]="$(wc "$wcparam" <<< "${list[$i]}")"
            [[ $wcparam == -c ]] && disp[$i]=$((${disp[$i]-1}))
            len="$((${disp[$i]}+3))"
            [[ $len -gt $w ]] && w=$len
        done
        cols=$((COLUMNS/w))
        [[ -n $max_cols && $cols -gt $max_cols ]] && cols=$max_cols
        [[ $cols -lt 1 ]] && cols=1
    else
        # too many items to calculate the width of each item
        cols=1
    fi
    rows=$(((list_size+cols-1)/cols))
    [[ $rows -ge $max_rows ]] && rows=$max_rows
    [[ $(((cols-1)*rows)) -ge $list_size ]] && cols=$((cols-1))
    if [[ $cols -eq 1 ]]; then
        max_cols=1
        max_rows=$list_size
    else
        max_rows=$rows
        max_cols=$(((list_size+rows-1)/rows))
    fi
    w=$((COLUMNS/cols))
    [[ $cols -gt 1 && $rows -lt $avail_rows ]] && rows=$avail_rows
    [[ ${#markers[@]} -gt 0 ]] && w=$((w-2))
    if [[ $cols -gt 1 ]]; then
        for ((i=0; i<list_size; i++)); do
            trail="$(printf "%$((w-${disp[$i]}))s" ' ')"
            item="${list[$i]}" && [[ $allow_escape -eq 0 ]] && item="${item//[^[:print:]]/^[}"
            disp[$i]="$item$trail"
        done
    else
        if [[ $__WRAP_OPTION_SUPPORTED__ -eq 0 ]]; then
            for ((i=0; i<list_size; i++)); do
                item="${list[$i]}" && [[ $allow_escape -eq 0 ]] && item="${item//[^[:print:]]/^[}"
                disp[$i]="${item:0:$((w-1))}"
            done
        elif [[ $allow_escape -eq 0 ]]; then
            disp=("${list[@]//[^[:print:]]/^[}")
        else
            disp=("${list[@]}")
        fi
    fi

    draw_line() {
        local i j c
        if [[ $1 -lt $list_size ]]; then
            for ((i=0; i<cols; i++)); do
                idx=$((($1+irow)+(i+icol)*rows))
                c=$'\e[0m'"${colors[$idx]}" && [[ $x == $i && $y == $1 ]] && c=$'\e[0;7m'"${colors[$idx]}"
                if [[ -n ${selected[$idx]} ]]; then
                    echo -ne $'\e[0m'"${markers[$idx]}$c*\e[33;48;5;239m" >&2
                    if [[ $cols -gt 1 ]]; then
                        echo -n "${disp[$idx]%?}"$'\e[0m' >&2
                    else
                        echo -n "${disp[$idx]}"$'\e[0m' >&2
                    fi
                elif [[ -n ${markers[$idx]} ]]; then
                    echo -ne $'\e[0m'"${markers[$idx]}$c" >&2
                    echo -n "${disp[$idx]}"$'\e[0m' >&2
                else
                    echo -ne "$c" >&2
                    echo -n "${disp[$idx]}"$'\e[0m' >&2
                fi
            done
        fi
        if [[ $1 -eq $((rows-1)) ]]; then
            get_cursor_pos
            [[ $__COL__ -lt $COLUMNS ]] && printf "%$((COLUMNS-__COL__+1))s" ' ' >&2
            echo -ne "$__NSH_DRAWLINE_END__" >&2
            draw_footer
            echo -ne "\e[${COLUMNS}D" >&2
            [[ $rows -gt 1 ]] && echo -ne "\e[$((rows-1))A" >&2
        else
            echo -e '\e[K' >&2
        fi
    }
    draw_footer() {
        local idx=$(((y+irow)+(x+icol)*rows))
        local lls=${#list_size}
        local lbs=$((lls*2+2))
        local bs="$(printf "%${lbs}s" ' ')" && bs="${bs//?/\\b}"
        local num_selected=${#selected[@]}
        if [[ -n $search ]]; then
            echo -ne "\e[${COLUMNS}D" >&2
            echo -ne "\e[0;39;41m$search" >&2
            printf "%$((COLUMNS-${#search}))s\e[0m" "[$list_size]" >&2
        elif [[ $show_footer -ne 0 && $idx -ge 0 && $idx -lt $list_size ]]; then
            if [[ $num_selected -gt 0 ]]; then
                local bs2="$(printf "%$((${#num_selected}+3))s" ' ')" && bs="$bs${bs2//?/\\b}"
                bs="$bs"$'\e[0;30;43m'"[*$num_selected]"
            fi
            printf "$bs\e[0;30;48;5;248m[%${lls}s/%${lls}s]\e[0m" $((idx+1)) $list_size >&2
        fi
    }
    print_selected() {
        local idx=$(((y+irow)+(x+icol)*rows))
        if [[ ${#selected[@]} -gt 0 ]]; then
            for ((i=0; i<list_size; i++)); do
                [[ -n ${selected[$i]} ]] && echo "${list[$i]}"
            done
        elif [[ $1 == force ]]; then
            echo "${list[$idx]}"
        fi
    }
    quit() {
        x=9999 y=9999
        for ((i=0; i<rows; i++)); do
            draw_line $i
        done
        [[ $rows -gt 1 ]] && echo -ne "\e[$((rows-1))B" >&2
        echo >&2
        return
    }

    echo -ne '\e[J' >&2
    for ((j=0; j<rows; j++)); do
        draw_line $j
    done

    move_cursor() {
        local xpre=$x ypre=$y icolpre=$icol irowpre=$irow
        local draw=1 && [[ $1 == --no-draw ]] && draw=0 && shift
        x=$((x+$1))
        y=$((y+$2))
        if [[ $1 -lt 0 ]]; then
            [[ $x -lt 0 ]] && icol=$((icol+x)) && x=0 && [[ $icol -lt 0 ]] && icol=0
        else
            [[ $x -ge $cols ]] && icol=$((icol+cols-x+1)) && x=$((cols-1)) && [[ $icol -gt $((max_cols-cols)) ]] && icol=$((max_cols-cols))
        fi
        if [[ $y -lt 0 ]]; then
            if [[ $((icol+x)) -gt 0 ]]; then
                x=$((x-1)) && y=$((rows-1))
                [[ $x -lt 0 ]] && x=0 && icol=$((icol-1)) && [[ $icol -lt 0 ]] && icol=0
            else
                irow=$((irow+y)) && y=0 && [[ $irow -lt 0 ]] && irow=0
            fi
        elif [[ $y -ge $rows ]]; then
            if [[ $cols -eq 1 ]]; then
                y=$((irow+y)) && [[ $y -ge $max_rows ]] && y=$((max_rows-1))
                irow=$((y-rows+1)) y=$((rows-1))
            else
                if [[ $((icol+x+1)) -lt $max_cols ]]; then
                    x=$((x+1)) && y=0
                    [[ $x -ge $cols ]] && x=$((cols-1)) && icol=$((icol+1)) && [[ $icol -gt $((max_cols-cols)) ]] && icol=$((max_cols-cols))
                else
                    irow=$((irow+rows-y+1)) && y=$((rows-1)) && [[ $irow -gt $((max_rows-rows)) ]] && irow=$((max_rows-rows))
                fi
            fi
        fi

        local newidx=$((irow+y+(icol+x)*rows))
        if [[ -n ${list[$newidx]} ]]; then
            if [[ $draw -ne 0 ]]; then
                if [[ $icolpre -ne $icol || irowpre -ne $irow ]]; then
                    for ((i=0; i<rows; i++)); do
                        draw_line $i
                    done
                else
                    if [[ $y -ne $ypre ]]; then
                        [[ $ypre -gt 0 ]] && echo -ne "\e[${ypre}B" >&2
                        draw_line $ypre
                        if [[ $ypre -ne $((rows-1)) ]]; then
                            echo -ne "\e[${COLUMNS}D" >&2
                            echo -ne "\e[$((ypre+1))A" >&2
                        fi
                    fi
                    [[ $y -gt 0 ]] && echo -ne "\e[${y}B" >&2
                    draw_line $y
                    if [[ $y -lt $((rows-1)) ]]; then
                        [[ $((rows-2-y)) -gt 0 ]] && echo -ne "\e[$((rows-2-y))B" >&2
                        echo -ne "\e[${COLUMNS}C" >&2
                        draw_footer
                        echo -ne "\e[${COLUMNS}D\e[$((rows-1))A" >&2
                    fi
                fi
            fi
        else
            x=$xpre y=$ypre icol=$icolpre irow=$irowpre
        fi
    }

    if [[ $initial -gt 0 ]]; then
        if [[ $cols -gt 1 ]]; then
            for ((i=0; i<initial; i++)); do move_cursor 0 1; done
        else
            move_cursor 0 $initial
        fi
    fi
    keys="${return_key[@]}"

    trap "resized=1" WINCH SIGWINCH

    while [[ $display -eq 0 ]]; do
        KEY="$NEXT_KEY" && NEXT_KEY= && [[ -z $KEY ]] && get_key KEY </dev/tty
        if [[ $resized -ne 0 ]]; then
            get_terminal_size </dev/tty
            get_cursor_pos </dev/tty
            avail_rows=$((LINES-__ROW__+1))
            if [[ $avail_rows -lt $rows ]]; then
                max_rows=$avail_rows
                rows=$max_rows
                [[ $cols -gt 1 ]] && cols=$((list_size/rows+1))
            fi
            for ((i=0; i<rows; i++)); do
                draw_line $i
            done
            resized=0
        fi
        local found=0
        local key_to_match="$KEY" && [[ $KEY == $'\e' ]] && key_to_match=$'\e '
        if [[ "$keys" == *$key_to_match* ]]; then
            idx=$(((y+irow)+(x+icol)*rows))
            item="${list[$idx]}"
            local quit=yes
            for ((i=0; i<${#return_key[@]}; i++)); do
                if [[ "${return_key[$i]}" == *"$key_to_match"* ]]; then
                    if [[ $(type -t "${return_fn[$i]}") == function ]]; then
                        "${return_fn[$i]}" "$item"
                    else
                        [[ ${return_fn[$i]} == *\.\.\. ]] && quit=no
                        eval "TEMPFUNC() { ${return_fn[$i]%\.\.\.}; }" >&2
                        TEMPFUNC "$item"
                    fi
                    found=1
                    break
                fi
            done
            hide_cursor >&2
            disable_echo >&2 </dev/tty
            [[ $found -ne 0 && $quit == yes ]] && break
        fi
        if [[ $found -eq 0 ]]; then
            case $KEY in
                l|$'\e[C')
                    x_old=$x
                    move_cursor 1 0
                    if [[ $cols -gt 1 && $x -eq $x_old ]]; then
                        if [[ $((irow+rows+(icol+x)*rows)) -lt $list_size ]]; then
                            for ((i=0; i<rows; i++)); do
                                move_cursor --no-draw 0 1
                            done
                            for ((i=0; i<rows; i++)); do
                                draw_line $i
                            done
                        fi
                    fi
                    ;;
                h|$'\e[D')
                    move_cursor -1 0
                    ;;
                j|$'\e[B')
                    move_cursor 0 1
                    ;;
                k|$'\e[A')
                    move_cursor 0 -1
                    ;;
                0)
                    if [[ $((x+icol)) -gt 0 ]]; then
                        move_cursor -$max_cols 0
                    else
                        x=0 icol=0 y=0 irow=0
                        for ((i=0; i<rows; i++)); do
                            draw_line $i
                        done
                    fi
                    ;;
                g)
                    x=0 y=0 icol=0 irow=0
                    for ((i=0; i<rows; i++)); do draw_line $i; done
                    ;;
                G)
                    if [[ $cols -gt 1 ]]; then
                        for ((i=0; i<$list_size; i++)); do move_cursor --no-draw 0 1; done
                        for ((i=0; i<$rows; i++)); do draw_line $i; done
                    else
                        move_cursor 0 $max_rows
                    fi
                    ;;
                ' ')
                    if [[ -n "$can_select" ]] && "$can_select" $idx "${list[$idx]}"; then
                        idx=$(((y+irow)+(x+icol)*rows))
                        if [[ -z "${selected[$idx]}" ]]; then
                            selected[$idx]="${list[$idx]}"
                        else
                            unset selected[$idx]
                            if [[ ${#selected[@]} -eq 0 ]]; then
                                # need to erase #selected from the footer
                                [[ $rows -gt 1 ]] && echo -ne "\e[$((rows-1))B" >&2
                                draw_line $((rows-1))
                            fi
                        fi
                    fi
                    if [[ $idx -lt $((list_size-1)) ]]; then
                        NEXT_KEY=j
                    else
                        # when idx == list_size-1, j key doesn't do anything
                        [[ $y -gt 0 ]] && echo -ne "\e[${y}B" >&2
                        draw_line $y
                        [[ $y -lt $((rows-1)) ]] && echo -ne "\e[$((y+1))A" >&2
                    fi
                    ;;
                $'\n'|$'\t')
                    print_selected force
                    break
                    ;;
                q|$'\e')
                    if [[ $KEY == $'\e' && ${#selected[@]} -gt 0 ]]; then
                        selected=()
                        NEXT_KEY=g
                    elif [[ $KEY == $'\e' && -n $search ]]; then
                        search=
                        list=("${list_org[@]}")
                        disp=("${disp_org[@]}")
                        colors=("${colors_org[@]}")
                        markers=("${markers_org[@]}")
                        list_size=${#list[@]}
                        x=0 y=0 icol=0 irow=0
                        for ((i=0; i<rows; i++)); do
                            draw_line $i
                        done
                    else
                        x=-1 # to lose focus
                        break
                    fi
                    ;;
                /)
                    list_org=("${list[@]}")
                    disp_org=("${disp[@]}")
                    colors_org=("${colors[@]}")
                    markers_org=("${markers[@]}")
                    list_size_org=${#list[@]}
                    x=99999 y=99999 icol=0 irow=0
                    search=/
                    while true; do
                        for ((i=0; i<rows; i++)); do
                            draw_line $i
                        done
                        get_key KEY </dev/tty
                        case $KEY in
                            $'\e'*|$'\t')
                                [[ $search == / ]] && search=
                                break
                                ;;
                            $'\177'|$'\b')
                                search="${search%?}"
                                [[ -z $search ]] && search=/
                                ;;
                            [[:print:]])
                                search="$search$KEY"
                                ;;
                        esac
                        item="${search#/}" && item="${item,,}"
                        item="$(fuzzy_word "${item//\ /}")"
                        list=() disp=() colors=() markers=()
                        for ((i=0; i<$list_size_org; i++)); do
                            if [[ "${list_org[$i],,}" == *$item* ]]; then
                                list+=("${list_org[$i]}")
                                disp+=("${disp_org[$i]}")
                                colors+=("${colors_org[$i]}")
                                markers+=("${markers_org[$i]}")
                            fi
                        done
                        list_size=${#list[@]}
                    done
                    x=0 y=0 icol=0 irow=0
                    for ((i=0; i<rows; i++)); do
                        draw_line $i
                    done
                    ;;
            esac
        fi
    done

    trap - WINCH SIGWINCH

    [[ $col0 -gt 1 ]] && echo -ne "\e[A\r\e[$((col0-1))C" >&2
    if [[ $display -eq 0 ]]; then
        echo -ne '\e[0m\e[J' >&2
        show_cursor >&2
    else
        echo -ne '\e[0m' >&2
    fi
    enable_echo >&2 </dev/tty
    enable_line_wrapping >&2
}

generate_new_filename() {
    [[ ! -e "$1" ]] && echo "$1" && return
    for i in {2..999999}; do
        if [[ ! -e "$1($i)" ]]; then
            echo "$1($i)"
            return
        fi
    done
}

cpmv() {
    local src dst src_name dst_name silent=0 i
    local op='cp -r' && [[ $1 == --mv ]] && op='mv'
    while true; do
        if [[ $1 == --cp ]]; then
            op='cp -r'
        elif [[ $1 == --mv ]]; then
            op=mv
        elif [[ $1 == --silent ]]; then
            silent=1
        else
            break
        fi
        shift
    done
    for dst in "$@"; do :; done
    while [[ $# -gt 1 ]]; do
        if [[ -e "$1" ]]; then
            src="$(sed 's/\/*$//' <<< "$1")"
            src_name="${src##*/}" && dst_name="$dst"
            if [[ -e "$dst" && -e "$dst/$src_name" ]]; then
                dst_name="$(generate_new_filename "$dst/$src_name")"
            fi
            [[ $silent -ne 0 ]] && echo -e "[${op%% *}] $(put_filecolor "$src")${src/#$HOME\//\~\/}\e[0m --> $dst_name"
            command $op "$src" "$dst_name"
        else
            echo "$1 does not exist" >&2
        fi
        shift
    done
}

ncp() {
    local op='command cp'
    local dst= && for dst in "$@"; do :; done
    local overwrite_all=no
    local skip_all=no
    local cancel=no
    local idx=0
    local num_thread=$(($(get_num_cpu)*2))
    local pids=()
    local LINE ans
    set +m
    [[ $1 == --overwrite ]] && overwrite_all=yes && shift
    local STAT_FSIZE_PARAM='--printf=%s'
    stat "$STAT_FSIZE_PARAM" . &>/dev/null || STAT_FSIZE_PARAM='-f%z'
    __worker() {
        local s="$1"
        local d="$2" && [[ -d "$d" ]] && d="$d/${s##*/}"
        if [[ -e "$d" ]]; then
            [[ -d "$d" ]] && return
            [[ $skip_all == yes ]] && return
            if [[ $overwrite_all == no ]]; then
                #$(command diff --brief "$s" "$d" &>/dev/null) && return
                local ssize=$(stat "$STAT_FSIZE_PARAM" "$s" 2>/dev/null)
                local dsize=$(stat "$STAT_FSIZE_PARAM" "$d" 2>/dev/null)
                local cmp="($(get_hsize $ssize) --> $(get_hsize $dsize))" && $(command diff --brief "$s" "$d" &>/dev/null) && cmp='(same file)'
                while true; do
                    echo "$NSH_PROMPT \"$d\" already exists $cmp"
                    ans="$(menu --color-func paint_cyan Overwrite Overwrite\ all Skip Skip\ all Rename Cancel)"
                    case "$ans" in
                        Overwrite) ;;
                        Overwrite\ all) overwrite_all=yes;;
                        #Skip) return;;
                        Skip\ all) skip_all=yes && return;;
                        Rename)
                            read_string --prefix "$NSH_PROMPT New name: " --initial "${d##*/}" LINE
                            [[ -n $LINE ]] && d="${d%/*}/$LINE" || continue
                            ;;
                        Cancel) cancel=yes; return;;
                        *)
                            echo "$NSH_PROMPT Skipped: $d"
                            return
                            ;;
                    esac
                    [[ $ans != 4 || ! -e "$d" ]] && break
                    echo "$NSH_PROMPT Sorry, $(print_filename "$d") also exists."
                done
            fi
        fi
        [[ -n "${pids[$idx]}" ]] && wait "${pids[$idx]}"
        $op "$s" "$d" &
        pids[$idx]="$!"
        ((idx++)) && [[ $idx -ge $num_thread ]] && idx=0
    }
    while [ $# -gt 1 ]; do
        [[ -z "$1" ]] && shift && continue
        if [[ -e "$1" ]]; then
            if [[ -d "$1" ]]; then
                local src="$(command cd "$1"; pwd -P)"
                local b="${src##*/}"
                local tmp="${1%/}" && tmp="$dst/${tmp##*/}"
                if [[ -d "$tmp" && $op == *cp ]]; then
                    local i= && for i in {1..99999}; do
                        if [[ ! -d "${tmp}_($i)" ]]; then
                            echo "$NSH_PROMPT $tmp already exists. changed name --> $tmp($i)"
                            eval "$op -r \"$1\" \"${tmp}($i)\""
                            break
                        fi
                    done
                elif [[ $op == *mv && ! -d "$dst/$b" ]]; then
                    eval "$op \"$1\" \"$dst\""
                else
                    if [[ -d "$dst" ]]; then
                        dst="$(command cd "$dst" &>/dev/null; pwd -P)"
                    else
                        mkdir -p "$dst"
                        b=
                    fi
                    mkdir -p "$dst/$b"
                    while read line; do
                        [[ $line == $src ]] && continue
                        local n="${line#$src/}"
                        if [[ -d "$line" ]]; then
                            mkdir -p "$dst/$b/$n"
                        else
                            __worker "$line" "$dst/$b/${n%/*}"
                            [[ $cancel == yes ]] && break
                        fi
                    done < <(find "$src" 2>/dev/null)
                    [[ $op == command\ mv && -e "$src" ]] && rm -rf "$src" &>/dev/null
                fi
            else
                __worker "$1" "$dst"
            fi
        else
            echo "$NSH_PROMPT $1: No such file or directory"
        fi
        [[ $cancel == yes ]] && break
        shift
    done
    unset __worker
    wait "${pids[@]}"
    [[ $cancel == yes ]] && echo "$NSH_PROMPT Cancelled"
    set -m
}
lksdjfhgsdr="$(type ncp | tail -n +2 | sed -e 's/ncp/nmv/' -e 's/command cp/command mv/')"
eval "$lksdjfhgsdr"
unset lksdjfhgsdr

ps() {
    local pid list line header word i0 i1
    if [[ $# -gt 0 || ! -t 0 || ! -t 1 ]]; then
        command ps "$@"
    else
        while true; do
            header= list=()
            while IFS=$'\n' read line; do
                [[ -z $header ]] && header="$line " && continue
                list+=("$line")
            done < <(command ps aux --sort -%cpu 2>/dev/null || command ps aux 2>/dev/null)
            echo "$header"
            line="$(menu -c 1 "${list[@]}")"
            echo -ne '\r\e[A\e[J'
            [[ -z "$line" ]] && break

            word='PID'
            i1="$(sed "s/\($word[ ]\+\).*/\1/" <<< "$header")"
            i0="${i1%$word*}" && i0="$(sed 's/[ ]*$//' <<< "$i0")"
            i0=${#i0} && i1=${#i1}
            pid="${line:$i0:$((i1-i0))}" && pid="$(strip_spaces "$pid")"

            line="$(command ps -p "$pid" | tail -n +2)"
            if [[ -n "$line" ]]; then
                echo -e "$NSH_PROMPT Kill the process $pid?"
                echo "$header"
                echo "$line"
                if [[ $(menu -r 1 OK Cancel) == OK ]]; then
                    kill -9 $pid
                else
                    echo -ne '\r\e[3A\e[J'
                fi
            fi
        done
    fi
}

get_hsize() {
    if [ $1 -ge 1073741824 ]; then
        local t=$(($1*10/1073741824))
        echo "$((t/10)).$((t%10)) G"
    elif [ $1 -ge 1048576 ]; then
        local t=$(($1*10/1048576))
        echo "$((t/10)).$((t%10)) M"
    elif [ $1 -ge 1024 ]; then
        local t=$(($1*10/1024))
        echo "$((t/10)).$((t%10)) K"
    elif [ $1 -ge 0 ]; then
        echo "$1 b"
    fi
}

disk() {
    __NSH_HIDE_ELAPSED_TIME__=1
    disable_line_wrapping
    local cur="$PWD"
    df -h .
    local bars=("          " "|         " "||        " "|||       " "||||      " "|||||     " "||||||    " "|||||||   " "||||||||  " "||||||||| " "||||||||||")
    local stat_param='--printf=%s'
    stat "$stat_param" . &>/dev/null || stat_param='-f%z'
    while true; do
        local l0=() && local l1=()
        local s0=() && local s1=()
        local total=0
        local ret
        while read f; do
            if [[ -d "$f" ]]; then
                if [[ "$f" == \.\. ]]; then
                    l0=("../" "${l0[@]}")
                    s0=("-1" "${s0[@]}")
                elif [[ "$f" != \. && "$f" != \.\. ]]; then
                    l0+=("$f/")
                    size=$(du -sk "$f" 2>/dev/null | cut -f 1)
                    size=$((size*1024))
                    total=$((total+size))
                    s0+=("$size")
                fi
            elif [[ -e "$f" ]]; then
                l1+=("$f")
                size=$(stat "$stat_param" "$f" 2>/dev/null)
                [[ -z $size ]] && size=0
                total=$((total+size))
                s1+=("$(stat "$stat_param" "$f" 2>/dev/null)")
            fi
        done < <(ls -a | sort --ignore-case)
        local files=("${l0[@]}" "${l1[@]}")
        local sideinfo=("${s0[@]}" "${s1[@]}")

        # sort by size
        local i j idx t
        for ((i=0; i<$((${#files[@]}-1)); i++)); do
            [[ ${files[$i]} == ../ ]] && continue
            idx=$i
            for ((j=$((i+1)); j<${#files[@]}; j++)); do
                [[ ${sideinfo[$j]} -gt ${sideinfo[$idx]} ]] && idx=$j
            done
            t=${sideinfo[$i]}
            sideinfo[$i]=${sideinfo[$idx]}
            sideinfo[$idx]=$t
            t="${files[$i]}"
            files[$i]="${files[$idx]}"
            files[$idx]="$t"
        done

        echo -e "\r\033[4m$NSH_COLOR_DIR$PWD\033[0m ($(get_hsize $total))\e[K"
        ret="$(for ((i=0; i<${#files[@]}; i++)); do
            local p='            ' && [[ ${sideinfo[$i]} -ge 0 ]] && p="[${bars[$(((${sideinfo[$i]}*100/$total+5)/10))]}]"
            printf "%8s %s\n" "$(get_hsize ${sideinfo[$i]})" "$p $(put_filecolor "${files[$i]}")${files[$i]}"
        done | menu --raw -c 1 --key $'\eq:' 'quit' | strip_escape)"
        ret="${ret#*\] }"
        [[ -z "$ret" ]] && break
        [[ -d "$ret" ]] && cd "$ret"
        echo -ne '\e[A'
    done
    cd "$cur"
    enable_line_wrapping
}

git_status()  {
    local line str= color=0
    local filenames=;
    local staged=0
    while read line; do
        case "$line" in
            *not\ a\ git*|*Not\ a\ git*|*Untracked*)
                break
                ;;
            *On\ branch*|*HEAD\ detached\ at*)
                str="${line##* }"
                color=32
                ;;
            *rebase\ in\ progress*)
                str="rebase-->${line##* }"
                color=31
                ;;
            *use*restore\ --staged*to\ unstage*)
                staged=1
                ;;
            *Changes\ not\ staged*)
                staged=0
                ;;
            *modified:*|*deleted:*|*new\ file:*|*renamed:*)
                color=91
                if [[ "$line" == *modified:* ]]; then
                    fname="$(echo $line | sed 's/.*modified:[ ]*//')"
                    if [[ "$line" == *both\ modified:* ]]; then
                        fname="!!$fname"
                    elif [[ "$line" == *modified:* ]]; then
                        if [[ $staged -ne 0 ]]; then
                            fname="++$fname"
                        elif [[ "$filenames;" == *\;++$fname\;* ]]; then
                            filenames="$filenames;"
                            filenames="${filenames//\;++$fname\;/\;}"
                        fi
                    fi
                    filenames="$filenames;$fname"
                elif [[ "$line" == *deleted* ]]; then
                    fname="$(echo $line | sed 's/.*deleted:[ ]*//')"
                    filenames="$filenames;$fname"
                elif [[ "$line" == *renamed* ]]; then
                    fname="${line#*-> }"
                    filenames="$filenames;$fname"
                fi
                ;;
            *Your\ branch*ahead*)
                line="${line% *}"
                line="${line##* }"
                str="$str +$line"
                color=33
                ;;
            *Your\ branch\ is\ behind*)
                line="${line#*by }"
                line="${line%% *}"
                str="$str -$line"
                color=33
                ;;
            *all\ conflicts*fixed*git\ rebase\ --continue*)
                str="run 'git rebase --continue'"
                ;;
            @@@ERROR@@@)
                return
                ;;
        esac
    done < <(LANGUAGE=en_US.UTF-8 command git status 2>&1 || echo @@@ERROR@@@)
    while read line; do
        filenames="$filenames;??$line"
    done < <(command git ls-files --others --exclude-standard 2>/dev/null | awk -F / '{print $1}' | uniq)
    if [[ -n $str ]]; then
        echo "$str"
        echo "$color"
        echo "$filenames;"
    fi
}

git() {
    local line op files remote file branch hash p skip_resolve=0
    __NSH_HIDE_ELAPSED_TIME__=1
    git_branch_name() {
        command git rev-parse --abbrev-ref HEAD 2>/dev/null
    }
    paint_cyan() {
        echo -e '\e[36m'
    }
    run() {
        [[ $1 == git ]] && shift
        echo -e "\r$(nsh_print_prompt)\e[0m\e[Kgit $@"
        eval command git "$@"
    }
    if [[ $1 == \-\- ]]; then
        shift
        files="$(printf '\"%s\" ' "$@")"
    elif [[ $# -gt 0 ]]; then
        command git "$@"
        return
    fi
    while true; do
        IFS=$'\n' read -d '' __GIT_STAT__ git_color __GIT_CHANGES__ < <(git_status)
        if [[ -z $__GIT_STAT__ ]]; then
            echo "$NSH_PROMPT This is not a git repository."
            read_command --prefix "$NSH_PROMPT To clone, enter the url: " --initial 'https://github.com/' line
            [[ -z $line ]] && return 1
            command git clone "$line"
            local dir="${line##*/}" && dir="${dir%.git}"
            [[ -d "$dir" ]] && command cd "$dir"
            return
        elif [[ "$__GIT_STAT__" == run*git\ rebase\ --continue* ]]; then
            run rebase --continue
        elif [[ $__GIT_CHANGES__ == *\;\!\!* && $skip_resolve -eq 0 ]]; then
            # having conflicts
            echo "$NSH_PROMPT Resolve conflicts first"
            while true; do
                files=()
                while read file; do
                    [[ $file == \!\!* ]] && files+=("${file#??}")
                done <<< "${__GIT_CHANGES__//;/$'\n'}"
                file="$(menu "${files[@]}" --color-func put_filecolor --marker-func git_marker)"
                [[ -z "$file" ]] && skip_resolve=1 && break
                $NSH_DEFAULT_EDITOR "$file"
                if [[ $(grep -c '^<\+ HEAD' "$file" 2>/dev/null) -eq 0 ]]; then
                    echo -n "$NSH_PROMPT $file was resolved. Stage the file? (y/n) "
                    get_key KEY; echo "$KEY"
                    [[ Yy == *$KEY* ]] && command git add "$file"
                    IFS=$'\n' read -d '' __GIT_STAT__ git_color __GIT_CHANGES__ < <(git_status)
                fi
            done
            if [[ $__GIT_CHANGES__ == *\!\!* ]]; then
                echo "$NSH_PROMPT Resolving conflicts was stopped"
                command git status
            else
                echo "$NSH_PROMPT All conflicts were resolved"
                command git commit
            fi
        else
            local branch="[$__GIT_STAT__]"
            local dst=
            local cnt
            op=
            color_branch_files() {
                if [[ $2 == 0 ]]; then
                    echo $'\e['"$git_color;4m"
                else
                    put_filecolor "$1"
                fi
            }
            if [[ -z "$files" ]]; then
                if [[ -n $__GIT_CHANGES__ ]]; then
                    IFS=\;$'\n' read -d '' -a files <<< "${__GIT_CHANGES__//\;[\?\!\+][\?\!\+]/\;}"
                    IFS=$'\n' read -d '' -a files < <(menu -c 1 "$branch" "${files[@]}" --select --color-func color_branch_files --marker-func git_marker)
                fi
                cnt=${#files[@]}
                if [[ $cnt -gt 0 ]]; then
                    for ((i=0; i<$cnt; i++)); do
                        if [[ "${files[$i]}" == "$branch" ]]; then
                            if [[ $cnt -eq 1 ]]; then
                                files=()
                                op=branch
                                break
                            else
                                unset files[$i]
                            fi
                        fi
                    done
                    [[ ${#files[@]} -gt 0 ]] && files="$(printf '\"%s\" ' "${files[@]}")"
                else
                    files=
                fi
            fi
            dst="${files/#\"/ }" && dst="${dst%%\"*}" && [[ "$files" == *\"\ \"* ]] && dst="$dst..."
            if [[ -n "$files" && "$files" != \. ]]; then
                op="$(menu "diff$dst" "commit$dst" "revert$dst" "stage$dst" "log$dst" --color-func paint_cyan --no-footer)"
                [[ -z "$op" ]] && files= && continue
                op="${op%% *}"
            else
                files=.
                [[ -z "$op" ]] && op="$(menu diff pull commit push revert log branch fetch --color-func paint_cyan --no-footer)"
                [[ -z "$op" ]] && return
            fi

            if [[ "$op" == diff ]]; then
                run diff "$files"
            elif [[ "$op" == pull ]]; then
                run pull origin "$(git_branch_name)"
            elif [[ "$op" == commit ]]; then
                run commit "$files"
            elif [[ "$op" == push ]]; then
                run push origin "$(git_branch_name)" -f
            elif [[ "$op" == revert ]]; then
                run checkout -- "$files"
            elif [[ "$op" == stage ]]; then
                run add "$files"
            elif [[ "$op" == log ]]; then
                p= && [[ $__WRAP_OPTION_SUPPORTED__ -ne 0 ]] && p='--color=always'
                while true; do
                    line="$(eval "command git log $p --decorate --oneline $files" | menu --raw -c 1 | strip_escape)"
                    if [[ -n "$line" ]]; then
                        hash="${line%% *}"
                        hash="$(sed 's/^[^0-9^a-z^A-Z]*//' <<< "$line")" && hash="${hash%% *}"
                        command git log --color=always -n 1 --stat "$hash"
                        op="$(menu -c 1 'Diff' 'Checkout this commit' 'Roll back to this commit' 'Roll back but keep the changes' 'Edit commit' --color-func paint_cyan --no-footer)"
                        if [[ "$op" == Diff ]]; then
                            run show "$hash"
                        elif [[ "$op" == Checkout* ]]; then
                            run checkout "$hash"
                            break
                        elif [[ "$op" == Roll\ back\ to* ]]; then
                            echo -n "$NSH_PROMPT You will lose the commits. Continue? (y/n) "
                            get_key KEY; echo "$KEY"
                            if [[ yY == *$KEY* ]]; then
                                run reset --hard "$hash"
                                break
                            fi
                        elif [[ "$op" == Roll\ back\ * ]]; then
                            echo -n "$NSH_PROMPT Roll back to this commit? You can cancel rollback by run "git restore FILE" and git pull (y/n) "
                            get_key KEY; echo "$KEY"
                            if [[ yY == *$KEY* ]]; then
                                run reset --soft $hash && run restore --staged .
                                break
                            fi
                        elif [[ "$op" == Edit* ]]; then
                            hash="$(command git log --oneline | grep -n "$hash")" && hash="${hash%%:*}"
                            run rebase -i "@~$hash"
                        fi
                    else
                        break
                    fi
                done
            elif [[ "$op" == branch ]]; then
                git_branch() {
                    while IFS=$'\n' read line; do
                        [[ "$line" != \(HEAD\ *detached\ * ]] && echo "$line"
                    done < <(LANGUAGE=en_US.UTF-8 command git branch 2>/dev/null | sed 's/[ *]*//')
                    for remote in $(command git remote 2>/dev/null); do
                        echo "$remote"
                        command git branch -r 2>/dev/null | sed 's/^[ *]*//' | grep "^$remote/" | sed -n '/ -> /!p'
                    done
                }
                while true; do
                    branch="$((echo '+ New branch'; git_branch) | menu -c 1 --color-func paint_cyan)"
                    if [[ "$branch" == '+ New branch' ]]; then
                        echo -n "$NSH_PROMPT New branch name: "
                        read_string line
                        if [[ -n "$line" ]]; then
                            run checkout -b "$line"
                            break
                        fi
                        echo -ne '\e[A\r\e[J'
                    elif [[ -n "$branch" ]]; then
                        line="$(menu Checkout Merge Browse Delete --color-func paint_cyan --no-footer)"
                        if [[ "$line" == Checkout ]]; then
                            echo -ne "$NSH_PROMPT Checkout $branch. Conintue(y/n)? "
                            get_key KEY && echo "$KEY"
                            [[ yY == *$KEY* ]] && run checkout "${branch#origin\/}" && break
                        elif [[ "$line" == Merge ]]; then
                            echo -ne "$NSH_PROMPT Merge $branch. Conintue(y/n)? "
                            get_key KEY && echo "$KEY"
                            [[ yY == *$KEY* ]] && run merge "${branch#origin\/}" && break
                        elif [[ "$line" == Browse ]]; then
                            local path=
                            selfn() {
                                [[ $1 == 0 ]] && return 1 || return 0
                            }
                            color_slash() {
                                [[ "$1" == */ ]] && echo "$NSH_COLOR_DIR$1"
                            }
                            sort_git_show() {
                                local line dirs=() files=()
                                while read line; do
                                    if [[ "$line" == */ ]]; then
                                        dirs+=("$line")
                                    else
                                        files+=("$line")
                                    fi
                                done
                                printf "%s\n" "${dirs[@]}"
                                printf "%s\n" "${files[@]}"
                            }
                            while true; do
                                echo "$NSH_PROMPT ${path:-$'\b'} on $branch"
                                local p="$(command git show --color=always "$branch:$path" | tail -n +2 | sort_git_show | menu -c 1 --color-func color_slash --can-select selfn --key H 'echo ..')"
                                [[ -z $p ]] && break
                                p="$(strip_escape "$p")"
                                if [[ "$p" == .. ]]; then
                                    path="$(sed 's/[^/]*\/$//' <<< "$path")"
                                    echo -ne '\e[A\r\e[J'
                                elif [[ "$p" == */ ]]; then
                                    [[ -z "$path" ]] && path="$p" || path="$path$p"
                                    echo -ne '\e[A\r\e[J'
                                else
                                    local name="$path${p#////}"
                                    op="$(menu "Checkout $name" "Diff $name" "Copy $name")"
                                    if [[ "$op" == Checkout* ]]; then
                                        run checkout "$branch" -- "$name"
                                    elif [[ "$op" == Diff* ]]; then
                                        run diff "$(git_branch_name)" "$branch" "$name"
                                    elif [[ "$op" == Copy* ]]; then
                                        local new_name="$(generate_new_filename "$name")"
                                        echo -e "\e[A$NSH_PROMPT $branch:$name --> $new_name\e[J"
                                        command git show "$branch:$name" > "$(generate_new_filename "$new_name")"
                                    else
                                        echo -ne '\e[A\r\e[J'
                                    fi
                                fi
                            done
                            echo -ne '\e[A\r\e[J'
                        elif [[ "$line" == Delete ]]; then
                            if [[ "$branch" == origin\/* ]]; then
                                echo -ne "$NSH_PROMPT \e[31m${branch#*/} branch will be deleted from repository. Continue? (y/n)\e[0m "
                                get_key KEY; echo "$KEY"
                                [[ yY == *$KEY* ]] && run push origin --delete "${branch#*/}"
                            else
                                echo -n "$NSH_PROMPT ${branch#*/} will be deleted from the disk. Continue? (y/n) "
                                get_key KEY; echo "$KEY"
                                [[ yY == *$KEY* ]] && run branch -D "$branch"
                            fi
                        fi
                    else
                        break
                    fi
                done
            elif [[ "$op" == fetch ]]; then
                run fetch
            else
                run $op "$files"
            fi
        fi
    done
}

play2048() {
    __NSH_HIDE_ELAPSED_TIME__=1
    local board=()
    local board_prev=()
    local board_hist=()
    local hlpos=()
    local buf=()
    local __COL__=$(get_cursor_col)
    init_board() {
        board=()
        if [[ $1 != force && -e ~/.cache/nsh/2048 ]]; then
            IFS=$'\n' read -d "" -ra board < ~/.cache/nsh/2048
        fi
        if [ ${#board[@]} -ne 16 ]; then
            board=()
            local i= && for i in {1..16}; do
                board+=("0")
            done
            add 2
            add 2
        fi
        board_prev=("${board[@]}")
        board_hist=()
        hlpos=()
    }
    display() {
        local r0=$(($(get_cursor_row)-4))
        local j= && for j in {0..3}; do
            move_cursor "$((r0+j));$__COL__"
            local i= && for i in {0..3}; do
                local n=${board[$((i+j*4))]}
                local c='36'
                case $n in
                    0) c='90';;
                    2) c='37';;
                    4) c='35';;
                    8) c='95';;
                    16) c='31';;
                    32) c='91';;
                    64) c='33';;
                    128) c='93';;
                    256) c='34';;
                    512) c='94';;
                    1024) c='32';;
                    2048) c='92';;
                esac
                [[ $n -eq 0 ]] && n=''
                [[ " ${hlpos[@]} " =~ " $((i+j*4)) " ]] && c="$c;7"
                printf '\e[0m %b%b%5s\e[0m' "\e[${c}m" $'\e[48;5;236m' "$n"
            done
            echo ' '
        done
        if [ ${#hlpos[@]} -gt 0 ]; then
            hlpos=()
            sleep 0.1
            display
        fi
    }
    add() {
        while true; do
            i=$(((RANDOM%4)+(RANDOM%4)*4))
            [[ ${board[$i]} -eq 0 ]] && board[$i]=${1:-2} && display && sleep 0.1 && break
        done
    }
    merge() {
        local i=$1
        local inc=$2
        buf=()
        local n= && for n in {1..4}; do
            [[ ${board[$i]} -ne 0 ]] && buf+=("${board[$i]}") && board[$i]=0
            i=$((i+inc))
        done
        buf+=(0 0 0 0)
        local prev=${buf[0]} && buf=("${buf[@]:1}")
        local dst=$1
        for n in {1..3}; do
            local cur=${buf[0]} && buf=("${buf[@]:1}")
            if [[ $cur -eq $prev ]]; then
                board[$dst]=$((prev+cur))
                prev=${buf[0]} && buf=("${buf[@]:1}")
                [[ $cur -ne 0 ]] && hlpos+=("$dst")
            else
                board[$dst]=$prev
                prev=$cur
            fi
            dst=$((dst+inc))
        done
        [[ $prev -ne 0 ]] && board[$dst]=$prev
    }

    hide_cursor
    echo; echo; echo; echo
    init_board
    while true; do
        display
        get_key KEY
        local new=2 && [[ $((RANDOM%10)) -eq 0 ]] && new=4
        board_prev=("${board[@]}")
        [[ ${#board_hist[@]} -gt 160 ]] && board_hist=("${board_hist[@]:${#board_hist[@]}-160}")
        hlpos=()
        case $KEY in
            $'\e'|'q')
                break
                ;;
            'r')
                init_board force
                ;;
            'u')
                i=$((${#board_hist[@]}-16))
                if [ $i -ge 0 ]; then
                    local n= && for ((n=0; n<16; n++)); do
                        board[$n]="${board_hist[$((i+n))]}"
                    done
                    board_prev=("${board[@]}")
                    board_hist=("${board_hist[@]:0:$i}")
                fi
                ;;
            'h'|$'\e[D')
                merge 0 1
                merge 4 1
                merge 8 1
                merge 12 1
                ;;
            'l'|$'\e[C')
                merge 3 -1
                merge 7 -1
                merge 11 -1
                merge 15 -1
                ;;
            'k'|$'\e[A')
                merge 0 4
                merge 1 4
                merge 2 4
                merge 3 4
                ;;
            'j'|$'\e[B')
                merge 12 -4
                merge 13 -4
                merge 14 -4
                merge 15 -4
                ;;
        esac
        [[ "${board[@]}" != "${board_prev[@]}" ]] && display && board_hist+=("${board_prev[@]}") && sleep 0.1 && add $new
    done
    printf '%s\n' "${board[@]}" > ~/.cache/nsh/2048
    echo -ne '\e[4A\e[J'
    show_cursor
}

read_string() {
    local prefix=
    local cmd=
    local cur=0
    local pre post cand word chunk
    local iword ichunk
    local KEY

    while true; do
        if [[ $1 == --prefix ]]; then
            prefix="$2"
            shift
            echo -ne "\r\e[0m$prefix" >&2
        elif [[ $1 == --initial ]]; then
            cmd="$2" && cur=${#cmd}
            shift
            echo -n "$cmd" >&2
        else
            break
        fi
        shift
    done
    iword=$cur && [[ "$cmd" == *\ * ]] && iword="${cmd% *} " && iword=${#iword}
    ichunk=$iword

    show_cursor
    while true; do
        pre="${cmd:0:$cur}"
        post="${cmd:$cur}"
        KEY="$NEXT_KEY" && NEXT_KEY= && [[ -z $KEY ]] && get_key KEY
        case $KEY in
            $'\e') # ESC
                if [[ -n $cmd ]]; then
                    cmd="$prefix$cmd" && echo -ne "${cmd//?/$'\b'}\r$prefix\e[J" >&2
                    cmd=
                    cur=0
                else
                    cmd=
                    break
                fi
                ;;
            $'\04') # ctrl+D
                echo '^C' >&2
                cmd=
                break
                ;;
            $'\n') # enter
                echo >&2
                break
                ;;
            $'\177'|$'\b') # backspace
                if [[ $cur -gt 0 ]]; then
                    echo -n $'\b \b'"$post ${post//?/$'\b'}"$'\b' >&2
                    cmd="${pre%?}$post"
                    cur=$((cur-1))
                    # update iword
                    if [[ $cur -le $iword ]]; then
                        pre="${cmd:0:$cur}"
                        if [[ $pre == *\ * ]]; then
                            pre="${pre% *} "
                            iword=${#pre}
                        else
                            iword=0
                        fi
                    fi
                fi
                ;;
            $'\e[3~') # del
                if [[ -n "$post" ]]; then
                    post="${post#?}"
                    echo -n "$post "$'\b'"${post//?/$'\b'}"
                    cmd="$pre$post"
                fi
                ;;
            $'\e[C') # right
                if [[ $cur -lt ${#cmd} ]]; then
                    echo -ne "${cmd:$cur:1}" >&2
                    cur=$((cur+1))
                    iword="${cmd:0:$cur}" && iword="${iword% *} " && iword=${#iword}
                    ichunk=$iword
                fi
                ;;
            $'\e[D') # left
                if [[ $cur -gt 0 ]]; then
                    echo -ne '\b' >&2
                    cur=$((cur-1))
                    iword="${cmd:0:$cur}" && iword="${iword% *} " && iword=${#iword}
                    ichunk=$iword
                fi
                ;;
            $'\e[1~'|$'\e[H') # home
                cur="$prefix$cmd" && echo -ne "${cur//?/$'\b'}\r$prefix" >&2
                cur=0
                ;;
            $'\e[4~'|$'\e[F') # end
                echo -ne "\e[$((${#prefix}+${#cmd}))D$prefix$cmd" >&2
                cur=${#cmd}
                ;;
            [[:print:]])
                cmd="$pre$KEY$post"
                cur=$((cur+1))
                if [[ $KEY == \  ]]; then
                    iword=$cur
                    ichunk=$cur
                fi
                echo -n "$KEY$post${post//?/$'\b'}" >&2
                ;;
        esac
    done
    printf -v "${1:-cmd}" "%s" "$cmd"
}

read_command() {
    local prefix=
    local cmd=
    local cur=0
    local pre post cand word chunk
    local iword ichunk
    local KEY

    while true; do
        if [[ $1 == --prefix ]]; then
            prefix="$2"
            shift
            echo -ne "\r\e[0m$prefix" >&2
        elif [[ $1 == --initial ]]; then
            cmd="$2" && cur=${#cmd}
            shift
            echo -n "$cmd" >&2
        else
            break
        fi
        shift
    done
    iword=$cur && [[ "$cmd" == *\ * ]] && iword="${cmd% *} " && iword=${#iword}
    ichunk=$iword

    show_cursor
    echo -ne '\e[J'
    while true; do
        pre="${cmd:0:$cur}"
        post="${cmd:$cur}"
        KEY="$NEXT_KEY" && NEXT_KEY= && [[ -z $KEY ]] && get_key KEY
        case $KEY in
            $'\e') # ESC
                if [[ -n $cmd ]]; then
                    cmd="$prefix$cmd" && echo -ne "${cmd//?/$'\b'}\r$prefix\e[J" >&2
                    cmd=
                    cur=0
                else
                    cmd=$'\e'
                    break
                fi
                ;;
            $'\04') # ctrl+D
                if [[ -n $cmd ]]; then
                    echo '^C' >&2
                    cmd=
                else
                    cmd='exit'
                    echo "$cmd" >&2
                fi
                break
                ;;
            $'\n') # enter
                echo "$post" >&2
                break
                ;;
            $'\177'|$'\b') # backspace
                if [[ $cur -gt 0 ]]; then
                    echo -n $'\b \b'"$post ${post//?/$'\b'}"$'\b' >&2
                    cmd="${pre%?}$post"
                    cur=$((cur-1))
                    # update iword
                    if [[ $cur -le $iword ]]; then
                        pre="${cmd:0:$cur}"
                        if [[ $pre == *\ * ]]; then
                            pre="${pre% *} "
                            iword=${#pre}
                        else
                            iword=0
                        fi
                    fi
                fi
                ;;
            $'\e[3~') # del
                if [[ -n "$post" ]]; then
                    post="${post#?}"
                    echo -n "$post "$'\b'"${post//?/$'\b'}"
                    cmd="$pre$post"
                fi
                ;;
            $'\t') # tab completion
                # ls abc/def/gh
                #    ^       ^
                #    iword   ichunk
                if [[ -z $cmd ]]; then
                    NEXT_KEY=$'\e'
                else
                    local quote=
                    while true; do
                        chunk="${pre:$ichunk}"
                        local p='-p' && [[ "$chunk" == */ ]] && p=  # to avoid //
                        cand="$(eval command ls $p -d "${pre:$iword:$((ichunk-iword))}$(fuzzy_word "${chunk:-*}")" 2>/dev/null | sed "s@^$HOME/@~/@" | sort --ignore-case --version-sort)"
                        if [[ "$cand" == *$'\n'* ]]; then
                            echo >&2
                            IFS=$'\n' read -d '' -a cand < <(echo -e "$cand" | menu --color-func put_filecolor --can-select select_file --key '.' 'echo "%&\$#!@"' --key $'\t' 'echo "$1"' --key $'\n' 'echo "////done////$1"')
                            echo -ne "\e[A${prefix//?/\\b}${pre//?/\\b}$prefix$pre" >&2
                        fi
                        if [[ ${#cand[@]} -le 1 ]]; then
                            cand="${cand[0]}"
                            if [[ $cand == "%&\$#!@" ]]; then
                                toggle_dotglob
                            elif [[ -n "$cand" ]]; then
                                local enter=0 && [[ "$cand" == ////done////* ]] && enter=1 && cand="${cand:12:$((${#cand}-12))}"
                                cand="${cand/#$HOME\//\~\/}"
                                word="${pre:$iword}"
                                echo -ne "${word//?/\\b}$cand" >&2
                                pre="${pre:0:$iword}$cand"
                                cmd="$pre$post"
                                cur=${#pre}
                                ichunk=$cur
                                [[ $enter -ne 0 ]] && NEXT_KEY=$'\n' && break
                                [[ -f "${word/#\~/$HOME}" ]] && NEXT_KEY=\  && break
                            else
                                break
                            fi
                        else
                            cand="$(printf '"%s" ' "${cand[@]}")"
                            word="${pre:$iword}"
                            echo -ne "${word//?/\\b}$cand" >&2
                            pre="${pre:0:$iword}$cand"
                            cmd="$pre$post"
                            cur=${#pre}
                            ichunk=$cur
                            break
                        fi
                    done
                    word="${pre:$iword}"
                    if [[ -e "$word" ]]; then
                        eval "[[ -e $word ]] && echo" &>/dev/null || quote=\"
                    fi
                    if [[ -n "$word" && -n $quote ]]; then
                        echo -ne "${word//?/\\b}$quote$word$quote$post" >&2
                        echo -ne "${post//?/\\b}" >&2
                        pre="${pre:0:$iword}$quote${pre:$iword}$quote"
                        cmd="$pre$post"
                        cur=${#pre}
                        NEXT_KEY=\ 
                    fi
                fi
                ;;
            $'\e[A') # up
                echo -e "${pre//?/\\b}\r$prefix\e[J" >&2
                cmd="$(menu "${history[@]}" -c 1 --initial "$HISTSIZE" --key ' ' 'echo "$1 "' --key $'\n' 'echo "////////$1"' --key $'\177'$'\b ' 'echo "${1%?}"')"
                [[ "$cmd" == ////////* ]] && cmd="${cmd:8:$((${#cmd}-8))}" && NEXT_KEY=$'\n'
                cur=${#cmd}
                echo -ne "\e[A${prefix//?/\\b}\r$prefix$cmd\e[J" >&2
                ;;
            $'\e[B') # down
                local d="$(menu -c 1 --raw "${bookmarks[@]/:/ $NSH_COLOR_DIR}")"
                [[ -n "$d" ]] && d="$(strip_escape "$d")" && cd "${d#* }"
                cmd=
                NEXT_KEY=$'\e'
                ;;
            $'\e[C') # right
                if [[ $cur -lt ${#cmd} ]]; then
                    echo -ne "${cmd:$cur:1}" >&2
                    cur=$((cur+1))
                    iword="${cmd:0:$cur}" && iword="${iword% *} " && iword=${#iword}
                    ichunk=$iword
                fi
                ;;
            $'\e[D') # left
                if [[ $cur -gt 0 ]]; then
                    echo -ne '\b' >&2
                    cur=$((cur-1))
                    iword="${cmd:0:$cur}" && iword="${iword% *} " && iword=${#iword}
                    ichunk=$iword
                fi
                ;;
            $'\e[1~'|$'\e[H') # home
                cur="$prefix$cmd" && echo -ne "${cur//?/$'\b'}\r$prefix" >&2
                cur=0
                ;;
            $'\e[4~'|$'\e[F') # end
                echo -ne "\e[$((${#prefix}+${#cmd}))D$prefix$cmd" >&2
                cur=${#cmd}
                ;;
            $'\e[21~') # F10
                cmd="$prefix$cmd" && echo -ne "${cmd//?/$'\b'}\r${prefix}\e[J" >&2
                cmd=nsh
                NEXT_KEY=$'\n'
                ;;
            $'\e'*)
                ;;
            [[:print:]])
                cmd="$pre$KEY$post"
                cur=$((cur+1))
                if [[ $KEY == \  ]]; then
                    iword=$cur
                    ichunk=$cur
                fi
                echo -n "$KEY$post${post//?/$'\b'}" >&2
                ;;
        esac
    done
    printf -v "${1:-cmd}" "%s" "$cmd"
}

# init
disable_line_wrapping
get_terminal_size
printf "%${COLUMNS}s" ' '
printf '\b\b\b    '
get_cursor_pos
echo -ne "\e[${COLUMNS}D"
__NSH_DRAWLINE_END__= && [[ $__COL__ -lt $COLUMNS ]] && __NSH_DRAWLINE_END__=$'\b'
printf "%$((COLUMNS+3))s" ' '
get_cursor_pos
if [[ $__COL__ -lt 5 ]]; then
    # terminal doesn't support disable_line_wrapping
    __WRAP_OPTION_SUPPORTED__=0
    echo -ne '\r\e[A'
else
    __WRAP_OPTION_SUPPORTED__=1
    echo -ne '\r'
fi
enable_line_wrapping
__NSH_HIDE_ELAPSED_TIME__=0

############################################################################
# main loop
############################################################################
nsh_main_loop() {
    local NSH_VERSION='0.3.1'
    local mode pw line
    local history=() history_size=0
    local bookmarks=() bookmark_size=0
    local prefix command ret
    local register register_op
    local trash_path=~/.cache/nsh/trash
    local tbeg telapsed
    local i KEY NEXT_KEY

    show_cursor
    enable_line_wrapping

    show_logo() {
        __NSH_HIDE_ELAPSED_TIME__=1
        disable_line_wrapping
        echo -e '                   _
 __               | |
 \ \     ____  ___| |__
  \ \   |  _ \/ __|  _ \
  / /   | | | \__ \ | | |
 /_/    |_| |_|___/_| |_| ' $NSH_VERSION
        echo " designed by naranicca"
        echo
        enable_line_wrapping
    }

    unset -f nsh
    nsh() {
        __NSH_HIDE_ELAPSED_TIME__=1
        local op="${1:-menu}"
        case "$op" in
            menu)
                local items=(Bookmarks) ret
                IFS=$'\n' read -d '' __GIT_STAT__ git_color __GIT_CHANGES__ < <(git_status)
                [[ -n "$__GIT_STAT__" ]] && items+=("Git")
                items+=(System Config 2048 about..)

                ret="$(menu --max-rows 1 "${items[@]}" --color-func paint_cyan --no-footer)"
                case "$ret" in
                    Bookmarks)
                        nsh bookmarks
                        ;;
                    Git)
                        git
                        ;;
                    System)
                        nsh system
                        echo
                        ;;
                    Config)
                        config
                        ;;
                    2048)
                        play2048
                        ;;
                    about..)
                        show_logo
                        ;;
                esac
                return
                ;;
            bookmarks)
                ret="$(menu -c 1 --raw "${bookmarks[@]/:/ $NSH_COLOR_DIR}" -c 1)"
                [[ -n "$ret" ]] && cd "$(strip_escape "${ret#??}")" && NEXT_KEY=$'\e'
                return
                ;;
            mark)
                line="$NSH_PROMPT Assign a key for bookmark: "
                echo -ne "$line"
                while true; do
                    get_key KEY
                    [[ -z $KEY || "$KEY" == $'\e' ]] && echo -ne "${line//?/\\b}\r\e[J" && return
                    if [[ "$KEY" == [a-zA-Z0-9] ]]; then
                        for ((i=0; i<${#bookmarks[@]}; i++)); do
                            if [[ "${bookmarks[$i]}" == "$KEY:"* ]]; then
                                echo $KEY
                                echo -ne "$NSH_PROMPT $KEY is already assigned to ${bookmarks[$i]#??}. Overwrite? (y/n) "
                                get_key KEY; echo "$KEY"
                                [[ yY == *$KEY* ]] && break
                                echo -ne "$NSH_PROMPT Assign another key: "
                                KEY= && break
                            fi
                        done
                        if [[ -n $KEY ]]; then
                            load_bookmarks
                            echo -e "${line//?/\\b}\r\e[0m$NSH_PROMPT Press \e[7m'$KEY\e[0m in explorer to jump to $(dirs +0)"
                            bookmarks+=("$KEY:$PWD")
                            printf '%s\n' "${bookmarks[@]}" > ~/.config/nsh/bookmarks
                            break
                        fi
                    fi
                done
                return
                ;;
            system)
                local pscmd='ps aux'
                local cpu mem cpu_activ_prev cpu_activ_cur cpu_total_prev cpu_total_cur
                local user nice system idle iowait irq softirq steal guest
                local line filesystem disk disk_size disk_used disk_avail disk_updated=0
                local list size dirs=() files=() sizes=()
                local str bs x=0 y=0 c0 c1 c2 cps cmd pid
                local t=10 i
                hide_cursor
                disable_line_wrapping
                ps aux --sort=-%cpu &>/dev/null && pscmd='ps aux --sort=-%cpu'
                while read line; do
                    if [[ -d "$line" ]]; then
                        dirs+=("$line/")
                    elif [[ -f "$line" ]]; then
                        files+=("$line")
                    fi
                done < <(command ls -d * 2>/dev/null | sort --ignore-case --version-sort)
                files=("${dirs[@]}" "${files[@]}")
                while true; do
                    if [[ $y -eq 0 ]]; then
                        # cpu usage
                        if read __cpu user nice system idle iowait irq softirq steal guest 2>/dev/null < /proc/stat; then
                            if [[ -z $cpu_activ_prev ]]; then
                                cpu_activ_prev=$((user+system+nice+softirq+steal))
                                cpu_total_prev=$((user+system+nice+softirq+steal+idle+iowait))
                                cpu=-
                                sleep 1s
                                continue
                            else
                                cpu_activ_cur=$((user+system+nice+softirq+steal))
                                cpu_total_cur=$((user+system+nice+softirq+steal+idle+iowait))
                                cpu=$((((cpu_activ_cur-cpu_activ_prev)*1000/(cpu_total_cur-cpu_total_prev)+5)/10))
                                cpu_activ_prev=$cpu_activ_cur
                                cpu_total_prev=$cpu_total_cur
                            fi
                        else
                            cpu=-
                        fi
                        # memory usage
                        mem=(`free -m 2>/dev/null | grep '^Mem:'`)
                        if [ -z "$mem" ]; then
                            mem=-
                        else
                            mem=$(((${mem[2]}*1000/${mem[1]}+5)/10))
                        fi
                        # disk usage
                        if [[ $t -eq 10 ]]; then
                            line="$(df -h . 2>/dev/null | tail -n 1)" && line="${line%%%*}"
                            IFS=\  read filesystem disk_size disk_used disk_avail disk <<< "$line"
                            t=0
                        fi
                        t=$((t+1))
                    fi
                    # process
                    get_terminal_size && size=$(($LINES*20/100))
                    c0=$'\e[0m' && [[ $x -eq 0 ]] && c0=$'\e[30;46m' && cmd="$pscmd"
                    c1=$'\e[0m' && [[ $x -eq 1 ]] && c1=$'\e[30;46m' && cmd="${pscmd/%cpu/%mem}"
                    c2=$'\e[0m' && [[ $x -eq 2 ]] && c2=$'\e[30;46m' && cmd='echo -e "        Size  Filename"; for ((i=0; i<${#files[@]}; i++)); do printf "%10s %s\n" "${sizes[$i]}" "${files[$i]}"; done'
                    if [[ $y -eq 0 ]]; then
                        list=()
                        while IFS= read line; do
                            list+=("$line")
                        done < <(eval "$cmd 2>/dev/null")
                    fi

                    printf '\r%bCPU: %3s%% \e[0m|%b MEM: %3s%% \e[0m|%b DISK: %s%% (%s/%s, %s free)\e[0m\e[J\n' $c0 $cpu $c1 $mem $c2 "$disk" "$disk_used" "$disk_size" "$disk_avail"
                    for ((i=0; i<size; i++)); do
                        cps=$'\e[0m  ' && [[ $i -eq $y ]] && cps=$'\e[31m> \e[92m'
                        if [[ $i -eq 0 ]]; then
                            echo -ne "\e[30;46m\e[K${list[$i]:0:$((COLUMNS-3))}\e[K\e[0m"
                        else
                            echo -ne "\n$cps${list[$i]:0:$((COLUMNS-3))}\e[K\e[0m"
                        fi
                    done
                    bs="${list[$((size-1))]//?/\\b}"

                    if [[ $x -eq 2 && $disk_updated -eq 0 ]]; then
                        get_key -t $__eps_get_key__ KEY
                        if [[ -z $KEY ]]; then
                            disk_updated=1
                            for ((i=0; i<${#files[@]}; i++)); do
                                [[ -z ${sizes[$i]} ]] && sizes[$i]="$(du -sh "${files[$i]}" 2>/dev/null | awk '{ printf $1 }')" && disk_updated=0 && break
                            done
                        else
                            get_key -t 2 KEY
                        fi
                    else
                        get_key -t 2 KEY
                    fi
                    case "$KEY" in
                        $'\e'|q)
                            [[ $y -eq 0 ]] && break
                            y=0
                            ;;
                        l|$'\e[C')
                            x=$((x+1)) && [[ $x -ge 3 ]] && x=0
                            y=0
                            ;;
                        h|$'\e[D')
                            x=$((x-1)) && [[ $x -lt 0 ]] && x=2
                            y=0
                            ;;
                        j|$'\e[B')
                            y=$((y+1)) && [[ $y -ge $size ]] && y=$((size-1))
                            ;;
                        k|$'\e[A')
                            y=$((y-1)) && [[ $y -lt 0 ]] && y=0
                            ;;
                        $'\n')
                            search_pid_from_header() {
                                local i=0
                                while [[ $# -gt 0 ]]; do
                                    [[ $1 == PID ]] && echo $i && return
                                    i=$((i+1))
                                    shift
                                done
                            }
                            i=$(search_pid_from_header ${list[0]})
                            line=(`echo ${list[$y]}`)
                            pid=${line[$i]}
                            echo -ne "\n$NSH_PROMPT Kill process $pid? (y/n) "
                            get_key KEY; echo -n "$KEY"
                            if [[ yY == *$KEY* ]]; then
                                kill -9 $pid || break
                            fi
                            echo -ne '\r\e[J\e[A'
                            ;;
                    esac
                    echo -ne "$bs\e[${size}A"
                done
                show_cursor
                enable_line_wrapping
                ;;
        esac
    }

    show_logo

    # load config
    config() {
        local config_file=~/.config/nsh/nshrc
        if [[ $1 == load ]]; then
            mkdir -p ~/.config/nsh
            [[ ! -e $config_file ]] && echo "$NSH_DEFAULT_CONFIG" > $config_file
        elif [[ $1 == default ]]; then
            echo "$NSH_DEFAULT_CONFIG" > $config_file
        else
            $NSH_DEFAULT_EDITOR "$config_file"
        fi
        source "$config_file"
    }
    config load
    # load bookmarks
    load_bookmarks() {
        touch ~/.config/nsh/bookmarks
        IFS=$'\n' read -d "" -ra bookmarks < <(cat ~/.config/nsh/bookmarks | sort)
    }
    load_bookmarks

    git_marker() {
        local m tmp p deep=0
        [[ $1 == --deep ]] && deep=1 && shift
        name="${1%/}"
        if [[ -z $__GIT_STAT__ ]]; then
            # not a git repository
            if [[ -d "$name" && -d "$name/.git" ]]; then
                m=$'\e[42m '
                if ! (command cd "$name"; command git diff --quiet 2>/dev/null); then
                    m=$'\e[41m '
                else
                    tmp="$(command cd "$name"; LANGUAGE=en_US.UTF-8 command git status -sb | head -n 1)"
                    p='\[(ahead|behind) [0-9]+\]$'
                    [[ "$tmp" =~ $p ]] && m=$'\e[43m '
                fi
                echo "$m"
            fi
        elif [[ $__GIT_CHANGES__ == *?\;* ]]; then
            [[ $deep -eq 1 && -d "$name" ]] && name="$name/" || name="$name;"
            if [[ "$__GIT_CHANGES__;" == *";$name"* ]]; then
                echo -e '\e[0;41m '
            elif [[ "$__GIT_CHANGES__;" == *";!!$name"* ]]; then
                echo -e '\e[37;41m!'
            elif [[ "$__GIT_CHANGES__;" == *";??$name"* ]]; then
                echo -e '\e[30;48;5;240m '
            elif [[ "$__GIT_CHANGES__;" == *";++$name"* ]]; then
                echo -e '\e[0;42m '
            else
                echo \ 
            fi
        fi
    }
    select_file() {
        [[ "$2" == '..' || "$2" == '../' ]] && return 1 || return 0
    }
    nsheval() {
        [[ $# -gt 0 ]] && command="$@"
        # save command to history
        history_size=${#history[@]}
        if [[ $history_size -eq 0 || "${history[$((history_size-1))]}" != "$command" ]]; then
            history+=("$command")
            local li=$((${#history[@]}-1))
            if [[ $li -ge 3 && "${history[$li]}" == "${history[$((li-2))]}" && "${history[$((li-1))]}" == "${history[$((li-3))]}" ]]; then
                history=("${history[@]:0:$((${#history[@]}-2))}")
            fi
            if [ ${#history[@]} -ge $HISTSIZE ]; then
                history=("${history[@]:$((${#history[@]}-HISTSIZE))}")
                history_idx=$((history_idx-${#history[@]}+HISTSIZE))
                [[ $history_idx -lt 0 ]] && history_idx=0
            fi
        fi
        # execute command
        [[ "$command" == '~' || "$command" == '~/'* ]] && command="$HOME/${command#?}"
        if [[ "$command" == */ && -d "$command" ]]; then
            command="cd $command"
        fi
        tbeg=$(get_timestamp)
        trap 'abcd &>/dev/null' INT
        eval "$command"
        ret=$?
        telapsed=$((($(get_timestamp)-tbeg+500)/1000))
        get_cursor_pos
        [[ $__COL__ -gt 1 ]] && echo $'\e[0;30;43m'"\n"$'\e[0m'
        [[ $ret -ne 0 ]] && ret=$'\e[0;31m'"[$ret returned]"$'\e[0m' || ret=
        if [[ $telapsed -gt 0 && $__NSH_HIDE_ELAPSED_TIME__ -eq 0 ]]; then
            local h=$((telapsed/3600))
            local m=$(((telapsed%3600)/60))
            local s=$((telapsed%60))
            ret+=$'\e[0;33m['
            [[ $h > 0 ]] && ret+="${h}h "
            [[ $h > 0 || $m > 0 ]] && ret+="${m}m "
            ret+="${s}s elapsed]"$'\e[0m'
        fi
        [[ -n $ret ]] && echo "$ret"$'\e[J'
        __NSH_HIDE_ELAPSED_TIME__=0
        command=
        trap - INT
    }
    trash() {
        [[ -n "$(ls -A "$trash_path" 2>/dev/null)" ]] && rm -rf "$trash_path"
        mkdir -p "$trash_path"
        mv "$@" "$trash_path" 2>/dev/null
        IFS=$'\n' read -d '' -a register < <(ls -d "$trash_path"/*)
        register_op=nmv
    }

    shopt -s nocaseglob
    update_dotglob()
    {
        if [[ $NSH_SHOW_HIDDEN_FILES -ne 0 ]]; then
            shopt -s dotglob
        else
            shopt -u dotglob
        fi
    }
    toggle_dotglob() {
        [[ $NSH_SHOW_HIDDEN_FILES -ne 0 ]] && NSH_SHOW_HIDDEN_FILES=0 || NSH_SHOW_HIDDEN_FILES=1
        update_dotglob
    }
    update_dotglob
    draw_titlebar() {
        prefix= && [[ -n $mode ]] && prefix="\e[0;30;45mSelect"
        echo -e "\r$prefix$(nsh_print_prompt)\e[J"
    }
    paint_cyan() {
        echo -e '\e[36m'
    }

    mode=first
    while true; do
        prefix="$(nsh_print_prompt)"
        if [[ $mode == first ]]; then
            command=$'\e'
            mode=
        else
            read_command --prefix "$prefix" --initial "$command" command
        fi

        if [[ "$command" == exit || "$command" == exit\ * ]]; then
            ret="$(strip_spaces "${command#exit}")"
            nsh() {
                nsh_main_loop "$@"
            }
            return "${ret:-0}"
        elif [[ "$command" == $'\e' ]]; then
            # explorer
            local line dirs files name path ret op
            local git_color
            local i
            while true; do
                IFS=$'\n' read -d '' __GIT_STAT__ git_color __GIT_CHANGES__ < <(git_status)
                [[ -n $__GIT_STAT__ ]] && __GIT_STAT__=$' \e[30;'"$((git_color+10))m($__GIT_STAT__)"$'\e[0m'
                draw_titlebar
                dirs=() files=()
                [[ "$(pwd)" != / ]] && dirs+=("../")
                while IFS= read line; do
                    if [[ -d "$line" ]]; then
                        dirs+=("$line/")
                    else
                        files+=("$line")
                    fi
                done < <(command ls -d * 2>/dev/null | sort --ignore-case --version-sort)
                local extra_params=()
                if [[ -z $mode ]]; then
                    extra_params+=(--key a 'echo ////add////' --key P 'echo ////fetch////' --can-select select_file --key $'\07' 'echo "////git////"' --key $'\e[21~' 'echo "////menu////"')
                elif [[ $mode == fetch ]]; then
                    extra_params+=(--can-select select_file)
                fi
                git_marker_deep() {
                    git_marker --deep "$@"
                }
                IFS=$'\n' read -d '' -a ret < <(menu "${dirs[@]}" "${files[@]}" --color-func put_filecolor --marker-func git_marker_deep --key $'\t' 'print_selected force' --key $'\n' 'echo ////enter////; print_selected force' --key '.' 'echo "////dotglob////"' --key '~' 'echo $HOME' --key r 'echo ./' --key ':' 'echo "////////"; print_selected; quit; echo >&2' --key H 'echo ../' --key y 'echo "////yank////"; print_selected force' --key p 'echo "////paste////"' --key d 'echo "////delete////"; print_selected force' --key i 'echo "////rename////"; echo "$1"; quit' --key - 'echo "////back////"' --key m 'echo "////mark////"' --key \' 'echo "////bookmark////"' "${extra_params[@]}")
                if [[ ${#ret[@]} -eq 0 ]]; then
                    [[ -n $mode ]] && echo -e "\e[A\e[A\r\e[J"
                    mode=
                    break
                elif [[ ${#ret[@]} -gt 1 || "${ret[0]}" == '////'* ]]; then
                    if [[ "${ret[0]}" == '////dotglob////' ]]; then
                        toggle_dotglob
                        ret=
                    elif [[ "${ret[0]}" == '////menu////' ]]; then
                        nsh menu
                        echo -e "\e[A"
                    else
                        if [[ "${ret[0]}" == '////enter////' ]]; then # enter key
                            unset ret[0]
                            if [[ $mode == add ]]; then
                                mode=
                                path="$(pwd)/${ret[1]%/}"
                                cd "$pwd"
                                echo -e "\e[A\e[A\e[0m\r$(nsh_print_prompt) ln -s $path ${path##*/}"
                                command ln -s "$path" "${path##*/}"
                            elif [[ $mode == fetch ]]; then
                                mode=
                                if [[ ${#ret[@]} -gt 1 ]]; then
                                    echo -e "\e[A\r$NSH_PROMPT Selected: ${ret[1]}...(${#ret[@]})\e[J"
                                else
                                    echo -e "\e[A\r$NSH_PROMPT Selected: ${ret[1]}\e[J"
                                fi
                                path="$(pwd)" && cd "$pwd"
                                op="$(menu Copy Move 'Symbolic Link' --color-func paint_cyan --no-footer)"
                                if [[ -n "$op" ]]; then
                                    echo -ne '\e[A'
                                    for ((i=1; i<=${#ret[@]}; i++)); do
                                        name="$path/${ret[$i]%/}"
                                        if [[ $op == Copy ]]; then
                                            echo "$NSH_PROMPT cp $name ."
                                            cp -r "$name" .
                                        elif [[ $op == Move ]]; then
                                            echo "$NSH_PROMPT mv $name ."
                                            mv "$name" .
                                        elif [[ $op == 'Symbolic Link' ]]; then
                                            echo "$NSH_PROMPT ln -s $name ${name##*/}"
                                            ln -s "$name" "${name##*/}"
                                        fi
                                    done
                                    echo
                                fi
                            elif [[ ${#ret[@]} -eq 1 && -d "${ret[1]}" ]]; then
                                cd "${ret[1]}"
                                break
                            else
                                for ((i=1; i<=${#ret[@]}; i++)); do
                                    [[ -x "${ret[$i]}" ]] && ret[$i]="./${ret[$i]}"
                                    eval "[[ -e ${ret[$i]} ]] && echo" &>/dev/null || ret[$i]=\"${ret[$i]}\"
                                done
                                ret="$(printf '%s ' "${ret[@]}")" && ret="${ret% }"
                                break
                            fi
                        elif [[ "${ret[0]}" == '////yank////' ]]; then
                            if [[ ${#ret[@]} -gt 1 ]]; then
                                local d="$(pwd)" && [[ $d == / ]] && d=
                                for ((i=1; i<${#ret[@]}; i++)); do
                                    ret[$i]="$d/${ret[$i]}"
                                done
                                unset ret[0]
                                register=("${ret[@]}")
                                register_op=ncp
                                if [[ ${#register[@]} -gt 1 ]]; then
                                    echo "$NSH_PROMPT yanked ${#register[@]} files"
                                else
                                    echo "$NSH_PROMPT yanked: ${register[0]/#$d\//}"
                                fi
                                echo
                            fi
                        elif [[ "${ret[0]}" == '////paste////' ]]; then
                            if [[ -n $register_op ]]; then
                                echo -e "\e[A${prefix//?/\\b}\r$(nsh_print_prompt)$register_op ${register[@]/#$HOME\//\~/} .\e[K"
                                $register_op "${register[@]}" .
                                echo
                                register=()
                                register_op=
                            fi
                        elif [[ "${ret[0]}" == '////delete////' ]]; then
                            unset ret[0]
                            line="${#ret[@]} files" && [[ ${#ret[@]} -eq 1 ]] && line="${ret[1]}"
                            echo -ne "\e[A\e[0;37;41m\e[KDelete $line? (yd/n)\e[0m " >&2
                            get_key KEY
                            draw_titlebar
                            [[ yYd == *$KEY* ]] && trash "${ret[@]}"
                        elif [[ "${ret[0]}" == '////add////' ]]; then
                            echo "$NSH_PROMPT Add:"
                            line=(Directory File) && [[ $mode != add ]] && line+=('Symbolic Link')
                            ret="$(menu "${line[@]}" --key d 'echo Directory' --key f 'echo File' --color-func paint_cyan --no-footer)"
                            if [[ $ret == Directory ]]; then
                                echo -ne "\e[A$NSH_PROMPT Add a directory: "
                                read_string name
                                [[ -n "$name" ]] && mkdir -p "$name"
                            elif [[ $ret == File ]]; then
                                echo -ne "\e[A$NSH_PROMPT Add a file: "
                                read_string name
                                [[ -n "$name" ]] && touch "$name"
                            elif [[ $ret == Symbolic\ Link ]]; then
                                echo -e "\e[A\e[A$NSH_PROMPT Adding a symbolic link of: \e[J"
                                echo
                                mode=add
                                pwd="$(pwd)"
                            else
                                echo -ne '\e[A'
                            fi
                        elif [[ "${ret[0]}" == '////fetch////' ]]; then
                            echo -e "\e[A$(nsh_print_prompt)fetch\n"
                            mode=fetch
                            pwd="$(pwd)"
                        elif [[ "${ret[0]}" == '////rename////' ]]; then
                            echo -n "$NSH_PROMPT rename: "
                            read_string --initial "${ret[1]}" line
                            [[ -n "$line" ]] && mv "${ret[1]}" "$line"
                        elif [[ "${ret[0]}" == '////git////' ]]; then
                            echo -e "\e[A\e[J$(nsh_print_prompt)git"
                            nsheval git
                            echo
                        elif [[ "${ret[0]}" == '////back////' ]]; then
                            cd - &>/dev/null
                        elif [[ "${ret[0]}" == '////mark////' ]]; then
                            nsh mark
                            echo
                        elif [[ "${ret[0]}" == '////bookmark////' ]]; then
                            get_key -t 1 KEY
                            if [[ -z $KEY ]]; then
                                menu -c 1 --raw "${bookmarks[@]/:/ $NSH_COLOR_DIR}" --display
                                get_key KEY
                            fi
                            for ((i=0; i<${#bookmarks[@]}; i++)); do
                                if [[ "${bookmarks[$i]}" == "$KEY:"* ]]; then
                                    cd "${bookmarks[$i]#??}"
                                    break
                                fi
                            done
                        else
                            [[ "${ret[0]}" == '////////' ]] && unset ret[0]
                            [[ ${#ret[@]} -gt 0 ]] && ret="$(printf '"%s" ' "${ret[@]}")" && ret="${ret% }"
                            break
                        fi
                    fi
                else
                    name="$(strip_escape "$ret")"
                    if [[ -d "$name" ]]; then
                        cd "$name"
                    else
                        line=("Edit $name" "Run $name" "Copy $name")
                        [[ $__GIT_CHANGES__ =~ \;[!]*"$name"\; ]] && line+=("Git: diff $name" "Git: stage $name" "Git: commit $name" "Git: revert $name" "Git...")
                        [[ $__GIT_CHANGES__ == *\;\?\?"$name"\;* ]] && line+=("Git: add $name")
                        local op="$(menu "${line[@]}" --color-func paint_cyan --no-footer)"
                        if [[ $op == Edit* ]]; then
                            $NSH_DEFAULT_EDITOR "$name"
                        elif [[ $op == Run* ]]; then
                            [[ -x "$name" ]] && name="./$name"
                            eval "[[ -e $name ]] && echo" &>/dev/null || name=\"$name\"
                            if [[ $name == *.py ]]; then
                                ret="python $name"
                            else
                                ret="$name"
                            fi
                            break
                        elif [[ $op == Copy* ]]; then
                            register=("$name")
                            register_op=ncp
                        elif [[ $op == Git:\ diff* ]]; then
                            echo -e "\e[A\r$(nsh_print_prompt)git diff $name\e[J"
                            git diff "$name"
                            git -- "$name"
                            echo
                        elif [[ $op == Git:\ add* || $op == Git:\ stage* ]]; then
                            echo -e "\e[A\r$(nsh_print_prompt)git add $name\e[J"
                            git add "$name"
                            git
                            echo
                        elif [[ $op == Git:\ commit* ]]; then
                            echo -e "\e[A\r$(nsh_print_prompt)git commit $name\e[J"
                            git commit "$name"
                            git
                            echo
                        elif [[ $op == Git:\ revert* ]]; then
                            echo -e "\e[A\r$(nsh_print_prompt)git checkout -- $name\e[J"
                            git checkout -- "$name"
                            git
                            echo
                        elif [[ $op == Git\.\.\. ]]; then
                            echo -e "\e[A\r$(nsh_print_prompt)git\e[J"
                            git -- "$name"
                            echo
                        else
                            ret=
                        fi
                    fi
                fi
                hide_cursor
                echo -ne "\e[A${prefix//?/\\b}\r\e[0m" >&2
            done
            hide_cursor
            echo -ne "\e[A${prefix//?/\\b}\r\e[0m" >&2
            [[ -n $ret ]] && command="$ret " || command=
        elif [[ -n $command ]]; then
            nsheval
        fi
    done
}

nsh() {
    nsh_main_loop "$@"
}

(return 0 2>/dev/null) || nsh_main_loop "$@"

