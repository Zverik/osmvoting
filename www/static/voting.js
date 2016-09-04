function radioClicked(e) {
    var radios = document.getElementsByName(e.target.name);
    for (var i = 0; i < radios.length; i++) {
        var div = radios[i].parentNode;
        if (radios[i] == e.target) {
            if (div.className.indexOf('voted') < 0)
                div.className = div.className + ' voted';
        } else {
            if (div.className.indexOf('voted') >= 0)
                div.className = div.className.replace(/voted/, '');
        }
    }
}

function addVoteListeners() {
    var inputs = document.getElementsByTagName('input');
    for (var i = 0; i < inputs.length; i++) {
        if (inputs[i].type == 'radio') {
            // Found a radio button, install a listener.
            inputs[i].onchange = radioClicked;
        }
    }
}

document.addEventListener('DOMContentLoaded', addVoteListeners);
