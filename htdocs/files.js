$(function(){
    if($('.file-card').length===0){$('.files-empty').show();return}

    var AudioCtx=window.AudioContext||window.webkitAudioContext;

    /* ---- Draw waveform on lower canvas ---- */
    function drawWaveform(canvas,data){
        var ctx=canvas.getContext('2d');
        var dw=canvas.offsetWidth,dh=canvas.offsetHeight;
        canvas.width=dw*2;canvas.height=dh*2;ctx.scale(2,2);
        var step=Math.max(1,Math.floor(data.length/dw));
        var bars=[],peak=0;
        for(var i=0;i<dw;i++){
            var s=i*step,e=Math.min(s+step,data.length),mx=0;
            for(var j=s;j<e;j++){var v=Math.abs(data[j]);if(v>mx)mx=v}
            bars.push(mx);if(mx>peak)peak=mx;
        }
        if(!peak)peak=1;
        ctx.fillStyle='#0a0e1a';ctx.fillRect(0,0,dw,dh);
        var mid=dh/2;
        for(var i=0;i<bars.length;i++){
            var val=bars[i]/peak,bH=val*(dh-2);
            var r,g,b;
            if(val<.5){r=0;g=Math.floor(100+val*310);b=255}
            else{r=Math.floor((val-.5)*510);g=255;b=Math.floor(255-(val-.5)*510)}
            ctx.fillStyle='rgb('+r+','+g+','+b+')';
            ctx.fillRect(i,mid-bH/2,1,bH||1);
        }
    }

    /* ---- Draw spectrogram on upper canvas ---- */
    function drawSpectrogram(canvas,data,sampleRate){
        var ctx=canvas.getContext('2d');
        var dw=canvas.offsetWidth,dh=canvas.offsetHeight;
        canvas.width=dw*2;canvas.height=dh*2;ctx.scale(2,2);
        ctx.fillStyle='#000';ctx.fillRect(0,0,dw,dh);

        var fftSize=256;
        var numCols=Math.min(dw,Math.floor(data.length/fftSize*2));
        if(numCols<2)return;
        var hopSize=Math.floor(data.length/numCols);
        var colW=dw/numCols;
        var halfFFT=fftSize/2;
        var hann=[];
        for(var i=0;i<fftSize;i++)hann.push(.5*(1-Math.cos(2*Math.PI*i/(fftSize-1))));

        for(var col=0;col<numCols;col++){
            var offset=col*hopSize;
            if(offset+fftSize>data.length)break;
            // Apply window and compute FFT magnitude
            var re=[],im=[];
            for(var i=0;i<fftSize;i++){re.push(data[offset+i]*hann[i]);im.push(0)}
            fft(re,im);
            for(var bin=0;bin<halfFFT;bin++){
                var mag=Math.sqrt(re[bin]*re[bin]+im[bin]*im[bin])/fftSize;
                var db=20*Math.log10(mag+1e-10);
                var norm=Math.max(0,Math.min(1,(db+60)/60));
                // Color: black→blue→cyan→yellow→white
                var r,g,b;
                if(norm<.25){r=0;g=0;b=Math.floor(norm*4*200)}
                else if(norm<.5){var t=(norm-.25)*4;r=0;g=Math.floor(t*255);b=200}
                else if(norm<.75){var t=(norm-.5)*4;r=Math.floor(t*255);g=255;b=Math.floor(200*(1-t))}
                else{var t=(norm-.75)*4;r=255;g=255;b=Math.floor(t*255)}
                ctx.fillStyle='rgb('+r+','+g+','+b+')';
                var y=dh-1-(bin/halfFFT)*dh;
                ctx.fillRect(col*colW,y,Math.ceil(colW)+1,Math.ceil(dh/halfFFT)+1);
            }
        }
    }

    /* ---- Cooley-Tukey FFT (in-place, radix-2) ---- */
    function fft(re,im){
        var n=re.length;
        if(n<=1)return;
        // Bit-reversal
        for(var i=1,j=0;i<n;i++){
            var bit=n>>1;
            for(;j&bit;bit>>=1)j^=bit;
            j^=bit;
            if(i<j){var t=re[i];re[i]=re[j];re[j]=t;t=im[i];im[i]=im[j];im[j]=t}
        }
        for(var len=2;len<=n;len<<=1){
            var ang=2*Math.PI/len;
            var wRe=Math.cos(ang),wIm=Math.sin(ang);
            for(var i=0;i<n;i+=len){
                var curRe=1,curIm=0;
                for(var j=0;j<len/2;j++){
                    var uRe=re[i+j],uIm=im[i+j];
                    var vRe=re[i+j+len/2]*curRe-im[i+j+len/2]*curIm;
                    var vIm=re[i+j+len/2]*curIm+im[i+j+len/2]*curRe;
                    re[i+j]=uRe+vRe;im[i+j]=uIm+vIm;
                    re[i+j+len/2]=uRe-vRe;im[i+j+len/2]=uIm-vIm;
                    var tRe=curRe*wRe-curIm*wIm;curIm=curRe*wIm+curIm*wRe;curRe=tRe;
                }
            }
        }
    }

    /* ---- Init each card ---- */
    $('.file-card').each(function(){
        var card=$(this),audio=card.find('audio')[0];
        var waveCanvas=card.find('.waveform-canvas')[0];
        var specCanvas=card.find('.spectrogram-canvas')[0];
        if(!audio||!waveCanvas)return;

        // Loading placeholder
        var wCtx=waveCanvas.getContext('2d');
        waveCanvas.width=waveCanvas.offsetWidth;waveCanvas.height=waveCanvas.offsetHeight;
        wCtx.fillStyle='#0a0e1a';wCtx.fillRect(0,0,waveCanvas.width,waveCanvas.height);
        wCtx.fillStyle='#335';wCtx.font='9px monospace';wCtx.fillText('Loading...',4,12);

        if(specCanvas){
            var sCtx=specCanvas.getContext('2d');
            specCanvas.width=specCanvas.offsetWidth;specCanvas.height=specCanvas.offsetHeight;
            sCtx.fillStyle='#000';sCtx.fillRect(0,0,specCanvas.width,specCanvas.height);
        }

        fetch(audio.src).then(function(r){return r.arrayBuffer()}).then(function(buf){
            var actx=new AudioCtx();
            return actx.decodeAudioData(buf).then(function(d){actx.close();return d});
        }).then(function(decoded){
            var pcm=decoded.getChannelData(0);
            drawWaveform(waveCanvas,pcm);
            if(specCanvas)drawSpectrogram(specCanvas,pcm,decoded.sampleRate);
        }).catch(function(){
            wCtx.fillStyle='#1a1a2e';wCtx.fillRect(0,0,waveCanvas.width,waveCanvas.height);
            wCtx.fillStyle='#555';wCtx.font='9px monospace';wCtx.fillText('n/a',4,12);
        });

        // Playback overlay
        var overlay=card.find('.waveform-overlay')[0];
        if(overlay){
            audio.addEventListener('timeupdate',function(){
                if(audio.duration>0)overlay.style.width=(audio.currentTime/audio.duration*100)+'%';
            });
            audio.addEventListener('ended',function(){overlay.style.width='0%'});
            // Click to seek
            card.find('.viz-wrap')[0].addEventListener('click',function(e){
                if(audio.duration){
                    var pct=(e.clientX-this.getBoundingClientRect().left)/this.offsetWidth;
                    audio.currentTime=pct*audio.duration;
                    if(audio.paused)audio.play();
                }
            });
        }
    });

    // Duration from metadata
    $('.file-card audio').each(function(){
        var a=this,dur=$(a).closest('.file-card').find('.dur');
        a.addEventListener('loadedmetadata',function(){
            if(a.duration&&isFinite(a.duration)&&dur.length){
                var s=Math.round(a.duration),t;
                if(s<60)t=s+'s';
                else if(s<3600)t=Math.floor(s/60)+'m'+('0'+(s%60)).slice(-2)+'s';
                else t=Math.floor(s/3600)+'h'+('0'+Math.floor((s%3600)/60)).slice(-2)+'m';
                dur.text(t);
            }
        });
    });

    // Delete
    $(document).on('click','.file-delete',function(e){
        e.preventDefault();e.stopPropagation();
        var name=$(this).data('name');
        if(!confirm('Eliminare '+name+'?'))return;
        var btn=$(this);btn.prop('disabled',true).text('...');
        $.ajax('/files/delete',{data:JSON.stringify({name:name}),contentType:'application/json',method:'POST'})
        .done(function(){
            btn.closest('.file-card').slideUp(150,function(){
                var grp=$(this).closest('.group-body');
                $(this).remove();
                if(grp.find('.file-card').length===0)grp.closest('.file-group').slideUp(150);
                if($('.file-card').length===0)$('.files-empty').show();
            });
        }).fail(function(){btn.prop('disabled',false).text('✕');alert('Errore')});
    });
});
